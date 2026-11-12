"""03_integrity_report.py — 每份文件的「解析完整性報告」。

用法：
    python scripts/03_integrity_report.py data/processed/cleaned/TH013.json
    python scripts/03_integrity_report.py data/processed/cleaned/          # 整個資料夾

輸入：01_extract 或 02_clean 輸出的 Document JSON（建議用 cleaned，會一併納入清洗階段的問題）。
輸出：
    data/review/<doc_id>.integrity_report.json   機器可讀
    data/review/<doc_id>.integrity_report.md     給人看的版本

報告內容：
- 每頁狀態（ok / needs_review / failed）、文字覆蓋率、待確認原因與區塊（含 bbox、失敗原因）
- 成功解析的表格（標題、欄名、列名、單位、偵測方法、覆蓋率）
- 無法解析的區域（圖片表格、圖片、向量圖表）
- 公式（是否連到 Equation 編號、是否需人工轉錄）
報告只彙整既有紀錄，不推測缺失內容。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from schemas import Document, TextBlock  # noqa: E402

RAW_DIR = ROOT / "data" / "raw"
DEFAULT_OUT_DIR = ROOT / "data" / "review"

UNPARSED_TYPES = {"image_region", "chart_region"}

# ---------------------------------------------------------------------------
# 文字品質掃描：只看一般文字區塊（公式、圖表文字本來就會有數學字元，已另外列管）
# ---------------------------------------------------------------------------

TEXT_TYPES = {"paragraph", "heading", "label", "list_item", "toc_entry", "figure_caption", "table_caption"}
GLYPH_RESIDUE_RE = re.compile(r"[\U0001D400-\U0001D7FF][\"#$!%&]")   # 例如 𝐿" 𝑋#：下標字形對應錯誤
MATH_ALNUM_RE = re.compile(r"[\U0001D400-\U0001D7FF]")
GLUED_TERM_RE = re.compile(r"\b(?:CIELAB|CIELab|Lab|sRGB|RGB|XYZ)[a-z]{4,}\b")  # 只用來定位片段；判定在 02_clean

TEXT_RULES = {
    "mass_glued_words": "大量黏字（疑似字間空白遺失，如 pypdf 的 Thispublicationcovers）",
    "very_long_token": "單一超長字串（≥25 字母），可能是黏字",
    "glued_term_in_source": "原文術語黏字（cleaned_text 照原文保留，search_text 已拆開）",
    "glyph_mapping_residue": "數學字型對應錯誤（下標變成 \" # $ 等符號），需對照原 PDF",
    "math_alphanumeric": "數學斜體字元（如 𝐿），cleaned_text 保留，search_text 已轉成一般字母",
}


def scan_text_quality(doc: Document) -> list[dict]:
    """逐區塊檢查常見的 PDF 文字層問題，回傳 [{rule, block_id, page_number, snippet}]。"""
    items = []
    for b in doc.blocks:
        if b.is_removed or b.block_type not in TEXT_TYPES or not b.cleaned_text:
            continue
        t = b.cleaned_text
        hits: list[tuple[str, int]] = []
        words = re.findall(r"[A-Za-z]+", t)
        if len(words) >= 10 and sum(len(w) >= 15 for w in words) / len(words) > 0.2:
            hits.append(("mass_glued_words", 0))
        hits += [("very_long_token", m.start()) for m in re.finditer(r"[A-Za-z]{25,}", t)]
        if "search:split_glued_term" in b.cleaning_ops:
            m = GLUED_TERM_RE.search(t)
            hits.append(("glued_term_in_source", m.start() if m else 0))
        hits += [("glyph_mapping_residue", m.start()) for m in GLYPH_RESIDUE_RE.finditer(t)][:1]
        if MATH_ALNUM_RE.search(t):
            hits.append(("math_alphanumeric", MATH_ALNUM_RE.search(t).start()))
        for rule, pos in hits:
            items.append({
                "rule": rule, "block_id": b.block_id, "page_number": b.page_number,
                "needs_review": b.needs_review,
                "snippet": t[max(0, pos - 40):pos + 60].replace("\n", " ⏎ "),
            })
    return items


def _caption_of(b: TextBlock, by_id: dict[str, TextBlock]) -> Optional[str]:
    for lid in b.linked_block_ids:
        x = by_id.get(lid)
        if x and x.block_type in ("table_caption", "figure_caption", "equation_label"):
            return " ".join(x.raw_text.split())
    return None


def review_block_entry(b: TextBlock, by_id: dict[str, TextBlock]) -> dict:
    """一個待確認區塊的可追溯資訊。"""
    return {
        "doc_id": b.doc_id,
        "block_id": b.block_id,
        "block_type": b.block_type,
        "region_kind": b.region_kind,
        "page_number": b.page_number,
        "bbox": list(b.bbox) if b.bbox else None,
        "source_locator": b.source_locator,
        "section": b.section,
        "parse_status": b.parse_status,
        "failure_reason": b.failure_reason,
        "review_reasons": b.review_reasons,
        "caption": _caption_of(b, by_id),
        "raw_text_preview": b.raw_text[:160],
    }


def page_statuses(doc: Document) -> list[dict]:
    """以擷取時的 PageReport 為基礎，再納入清洗階段新增的問題，重新判定每頁狀態。"""
    by_id = {b.block_id: b for b in doc.blocks}
    out = []
    for pr in doc.pages:
        blocks = [b for b in doc.blocks if b.page_number == pr.page_number and not b.is_removed]
        issues = [i for i in doc.issues if i.page_number == pr.page_number and not i.resolved]
        review = [b for b in blocks if b.needs_review]
        reasons = sorted(set(pr.reasons) | {i.issue_type for i in issues if i.severity in ("warning", "error")}
                         | {i.issue_type for i in issues if i.block_id in {b.block_id for b in review}})
        if pr.status == "failed":
            status = "failed"
        elif review or any(i.severity in ("warning", "error") for i in issues):
            status = "needs_review"
        else:
            status = "ok"
        out.append({
            "page_number": pr.page_number,
            "status": status,
            "text_coverage": pr.text_coverage,
            "text_chars": pr.text_chars,
            "tables_parsed": pr.tables_parsed,
            "tables_failed": pr.tables_failed,
            "image_regions": pr.image_regions,
            "chart_regions": pr.chart_regions,
            "formulas": pr.formulas,
            "reasons": reasons,
            "review_blocks": [review_block_entry(b, by_id) for b in review],
        })
    return out


def build_integrity_report(doc: Document) -> dict:
    """彙整整份文件的完整性報告（dict，可直接存成 JSON）。"""
    m = doc.manifest
    by_id = {b.block_id: b for b in doc.blocks}
    live = [b for b in doc.blocks if not b.is_removed]
    pages = page_statuses(doc)
    tables = [b for b in live if b.block_type == "table"]
    unparsed = [b for b in live if b.block_type in UNPARSED_TYPES or (b.parse_status == "failed" and b.block_type not in ("table",))]
    unresolved_caps = [b for b in live if b.block_type == "table_caption" and b.needs_review]
    formulas = [b for b in live if b.block_type == "formula"]
    st = Counter(p["status"] for p in pages)
    text_quality = scan_text_quality(doc)
    return {
        "doc_id": m.doc_id,
        "source_path": m.source_path,
        "source_type": m.source_type,
        "sha256": m.sha256,
        "stage": doc.stage,
        "summary": {
            "pages_processed": len(m.pages_processed),
            "pages_ok": st.get("ok", 0),
            "pages_needs_review": st.get("needs_review", 0),
            "pages_failed": st.get("failed", 0),
            "min_text_coverage": min((p["text_coverage"] for p in pages), default=None),
            "blocks": len(doc.blocks),
            "blocks_needing_review": sum(b.needs_review for b in live),
            "tables_parsed": len(tables),
            "tables_parsed_needing_review": sum(b.needs_review for b in tables),
            "tables_unparsed": sum(b.region_kind == "table_image" for b in unparsed) + len(unresolved_caps),
            "image_regions": sum(b.block_type == "image_region" for b in live),
            "chart_regions": sum(b.block_type == "chart_region" for b in live),
            "formulas": len(formulas),
            "formulas_needing_review": sum(b.needs_review for b in formulas),
            "open_issues_by_type": dict(sorted(Counter(i.issue_type for i in doc.issues if not i.resolved).items())),
            "text_quality_by_rule": {r: sum(x["rule"] == r for x in text_quality) for r in TEXT_RULES},
        },
        "text_quality": text_quality,
        "pages_needing_review": [p["page_number"] for p in pages if p["status"] != "ok"],
        "pages": pages,
        "tables_parsed": [
            {
                "block_id": b.block_id, "page_number": b.page_number, "bbox": list(b.bbox) if b.bbox else None,
                "source_locator": b.source_locator,
                "caption": b.table.caption if b.table else None,
                "method": b.table.detection_method if b.table else None,
                "n_rows": b.table.n_rows if b.table else None, "n_cols": b.table.n_cols if b.table else None,
                "column_names": b.table.column_names if b.table else None,
                "row_labels": b.table.row_labels if b.table else None,
                "units": b.table.units if b.table else {},
                "empty_cells": b.table.empty_cells if b.table else None,
                "text_coverage": b.table.text_coverage if b.table else None,
                "parse_status": b.parse_status,
                "needs_review": b.needs_review, "review_reasons": b.review_reasons,
            }
            for b in tables
        ],
        "unparsed_regions": [review_block_entry(b, by_id) for b in unparsed + unresolved_caps],
        "formulas": [
            {**review_block_entry(b, by_id), "raw_text": b.raw_text} for b in formulas
        ],
    }


def _ranges(nums: list[int]) -> str:
    """[1,2,3,5,7,8] → '1–3, 5, 7–8'"""
    if not nums:
        return "—"
    out, start, prev = [], nums[0], nums[0]
    for n in nums[1:] + [None]:
        if n is not None and n == prev + 1:
            prev = n
            continue
        out.append(f"{start}–{prev}" if start != prev else f"{start}")
        if n is not None:
            start = prev = n
    return ", ".join(out)


KIND_LABEL = {
    "table_image": "圖片表格", "figure_image": "圖片（有圖說）", "image": "圖片（無標題）",
    "chart": "向量圖表", "formula": "公式", "table": "表格",
}


def render_markdown(rep: dict) -> str:
    """把 build_integrity_report 的結果轉成 Markdown。"""
    s = rep["summary"]
    ok_pages = [p["page_number"] for p in rep["pages"] if p["status"] == "ok"]
    L = [
        f"# 解析完整性報告：{rep['doc_id']}",
        "",
        f"- 來源：`{rep['source_path']}`（{rep['source_type']}，sha256 `{rep['sha256'][:12]}…`，stage={rep['stage']}）",
        f"- 頁數：{s['pages_processed']}；ok {s['pages_ok']}／待確認 {s['pages_needs_review']}／失敗 {s['pages_failed']}",
        f"- 最低文字覆蓋率：{s['min_text_coverage'] if s['min_text_coverage'] is not None else '—'}",
        f"- 表格：成功解析 {s['tables_parsed']}（其中待確認 {s['tables_parsed_needing_review']}）；無法解析 {s['tables_unparsed']}",
        f"- 公式：{s['formulas']}（待人工確認 {s['formulas_needing_review']}）；圖片區域 {s['image_regions']}；向量圖表 {s['chart_regions']}",
        f"- 未解決 issue：{', '.join(f'`{k}`×{v}' for k, v in s['open_issues_by_type'].items()) or '無'}",
        "",
        "## 待人工確認的頁面",
        "",
    ]
    review_pages = [p for p in rep["pages"] if p["status"] != "ok"]
    if not rep["pages"]:
        L.append("（HTML 文件沒有頁碼，待確認區塊見下方各節）")
    elif not review_pages:
        L.append("（無）")
    else:
        L += ["| 頁 | 狀態 | 覆蓋率 | 原因 | 區塊（類型 · 失敗原因） |", "|---|---|---|---|---|"]
        for p in review_pages:
            items = "<br>".join(
                f"`{b['block_id'].rsplit('_', 2)[-2] + '_' + b['block_id'].rsplit('_', 1)[-1]}` "
                f"{KIND_LABEL.get(b['region_kind'], b['block_type'])}"
                + (f"（{b['caption'][:40]}）" if b["caption"] else "")
                + f" · {(b['failure_reason'] or '; '.join(b['review_reasons']))[:70]}"
                for b in p["review_blocks"]
            )
            L.append(f"| {p['page_number']} | {p['status']} | {p['text_coverage']:.3f} | {', '.join(p['reasons'])} | {items} |")
    L += ["", f"無需確認的頁面：{_ranges(ok_pages)}", "", "## 成功解析的表格", ""]
    if not rep["tables_parsed"]:
        L.append("（無：本文件沒有可從文字層解析的表格）")
    for t in rep["tables_parsed"]:
        L += [
            f"### p.{t['page_number'] or '-'} · {t['caption'] or '（無標題）'}",
            f"- block：`{t['block_id']}`；方法：{t['method']}；{t['n_rows']}×{t['n_cols']}；bbox {t['bbox']}",
            f"- 欄名：{t['column_names']}",
            f"- 列名：{t['row_labels']}",
            f"- 單位：{t['units'] or '（表格中未標示）'}；空白儲存格：{t['empty_cells']}；覆蓋率：{t['text_coverage']}",
            f"- 待確認：{'；'.join(t['review_reasons']) if t['needs_review'] else '否'}",
            "",
        ]
    L += ["## 無法解析的區域", ""]
    if not rep["unparsed_regions"]:
        L.append("（無）")
    else:
        L += ["| 頁 | 類型 | 標題 | bbox | 原因 |", "|---|---|---|---|---|"]
        for u in rep["unparsed_regions"]:
            L.append(f"| {u['page_number'] or u['source_locator']} | {KIND_LABEL.get(u['region_kind'], u['block_type'])} | "
                     f"{(u['caption'] or '—')[:50]} | {u['bbox']} | {(u['failure_reason'] or '; '.join(u['review_reasons']))[:80]} |")
    L += ["", "## 文字品質掃描", "",
          "只列出一般文字區塊中的可疑處；不需要逐頁閱讀全文，只需看這張表。", ""]
    tq = rep["text_quality"]
    L += [f"- {desc}：{s['text_quality_by_rule'][rule]} 處" for rule, desc in TEXT_RULES.items()]
    if tq:
        L += ["", "| 頁 | 規則 | block | 已標待確認 | 片段 |", "|---|---|---|---|---|"]
        for x in tq:
            snippet = x["snippet"].replace("|", "\\|")  # f-string 內不能有反斜線（Python 3.11）
            L.append(f"| {x['page_number'] or '-'} | {x['rule']} | `{x['block_id']}` | {'是' if x['needs_review'] else '否'} | "
                     f"{snippet} |")
    L += ["", "## 公式", ""]
    if not rep["formulas"]:
        L.append("（無）")
    for f in rep["formulas"]:
        L += [
            f"- p.{f['page_number']} `{f['block_id']}` {f['caption'] or '（未連到編號）'} · {f['parse_status']}"
            + (f" · {f['failure_reason']}" if f["failure_reason"] else ""),
            "  ```text", *("  " + ln for ln in f["raw_text"].splitlines()), "  ```",
        ]
    return "\n".join(L) + "\n"


def ensure_outside_raw(path: Path) -> None:
    p, raw = path.resolve(), RAW_DIR.resolve()
    if p == raw or raw in p.parents:
        raise ValueError(f"拒絕寫入原始資料夾：{p}")


def iter_inputs(paths: Iterable[Path]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        out += sorted(p.glob("*.json")) if p.is_dir() else [p]
    return out


def run(inputs: list[Path], out_dir: Path) -> list[Path]:
    ensure_outside_raw(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for path in iter_inputs(inputs):
        doc = Document.load(path)
        rep = build_integrity_report(doc)
        j = out_dir / f"{doc.manifest.doc_id}.integrity_report.json"
        md = out_dir / f"{doc.manifest.doc_id}.integrity_report.md"
        j.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
        md.write_text(render_markdown(rep), encoding="utf-8")
        s = rep["summary"]
        print(f"[integrity] {doc.manifest.doc_id}: pages ok={s['pages_ok']} review={s['pages_needs_review']} "
              f"failed={s['pages_failed']} | tables parsed={s['tables_parsed']} unparsed={s['tables_unparsed']} "
              f"| formulas review={s['formulas_needing_review']}/{s['formulas']}")
        print(f"            review pages: {_ranges(rep['pages_needing_review']) if rep['pages'] else '(html)'}")
        print(f"            text quality: {', '.join(f'{k}={v}' for k, v in s['text_quality_by_rule'].items())}")
        written += [j, md]
    return written


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="LipMatch AI 解析完整性報告")
    ap.add_argument("inputs", nargs="+", type=Path, help="Document JSON 檔或資料夾")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR)
    args = ap.parse_args(argv)
    run(args.inputs, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
