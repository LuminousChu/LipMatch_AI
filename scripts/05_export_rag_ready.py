"""05_export_rag_ready.py — 匯出可進 RAG 的段落，每段附完整出處。

用法：
    python scripts/05_export_rag_ready.py data/processed/cleaned/
    python scripts/05_export_rag_ready.py data/processed/cleaned/TH013.json

輸入：02_clean 輸出的 Document JSON；書目資訊來自 config/sources.json。
輸出：
    data/processed/rag_ready/<doc_id>.jsonl     每行一段：搜尋用文字、原文、頁碼、章節、引用字串
    data/processed/rag_ready/<doc_id>.summary.md 可用／排除統計（排除原因可稽核）

篩選規則只定義在 lipmatch/eligibility.py。
文件不在 config/sources.json 時直接報錯：沒有出處的段落不該進 RAG。

每筆紀錄的補充欄位：
- 被分頁／換欄／圖表切斷的段落（02_clean 設定的 continues_from_block_id）合併成一筆：
  block_ids 列出所有原始區塊，pages 為頁碼範圍，可逐段追溯。
- 圖說與表格標題的 data_status：圖說只描述圖，不代表圖中數值已取得（例如「53 種唇色」的色票在圖片裡）。
- attribution：有引用標記 → cites_other_work；沒有 → no_citation_marker（不等於本研究結論，只是沒找到標記）。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lipmatch.eligibility import classify_blocks, practical_activities  # noqa: E402
from lipmatch.sources import DEFAULT_SOURCES, SourceInfo, load_sources, reference_string, short_citation  # noqa: E402
from schemas import Document, TextBlock  # noqa: E402

DEFAULT_OUT_DIR = ROOT / "data" / "processed" / "rag_ready"

REASON_LABEL = {
    "removed": "已移除（頁首頁尾等雜訊）",
    "no_text": "沒有文字（圖片、圖表區域）",
    "needs_review": "待人工確認",
    "table_of_contents": "目錄",
    "not_content": "標題／標籤（已記在 section）",
    "front_matter": "前言（標題頁、聲明、致謝）",
    "excluded_section": "參考文獻、縮寫表等",
    "keywords": "關鍵字清單",
    "exercise": "教材題組（測驗、答案、學習目標，及結構不完整的活動）",
    "practical_activity_repeat": "教材實作活動：步驟與正文方法重複，附在正文紀錄當第二定位（corroborating_units），不另成段",
    "practical_activity_unmatched": "教材實作活動：結構完整但對不到可匯出的正文方法，未經內容核定不匯出",
    "boilerplate": "出版資訊（授權聲明、版權、通訊作者等）",
}

CONTINUABLE = {"paragraph", "list_item"}
CAPTION_TYPES = {"figure_caption", "table_caption"}
DATA_STATUS_NOTE = {
    "not_extracted": "這是圖說／表格標題，對應的圖片或圖表內容（例如色票、數據點）沒有被擷取，不可視為已取得完整數據",
    "extracted": "對應的表格已解析並收錄，數值見 linked_block_ids 中的 table 區塊",
    "extracted_pending_review": "對應的表格已解析，但仍待人工確認，數值尚未收錄進 RAG",
    "unknown": "這是圖說／表格標題，找不到對應的圖表區域，數據是否存在需對照原文",
}


def _join(a: str, b: str) -> str:
    """合併被切斷的兩段：中文之間不加空白；前段以連字號結尾直接相接；其餘加一個空白。"""
    a, b = (a or "").rstrip(), (b or "").lstrip()
    if not a or not b:
        return a + b
    if a.endswith("-") or ("\u3400" <= a[-1] <= "\u9fff" and "\u3400" <= b[0] <= "\u9fff"):
        return a + b
    return a + " " + b


def build_chains(doc: Document, reasons: dict) -> list[list[TextBlock]]:
    """可用區塊依閱讀順序串成「段落鏈」：continues_from_block_id 指向上一段、且上一段也可用時接在一起。
    中間有一段被排除（例如待確認），鏈就斷開，各自成一筆（continues_from_block_id 仍保留供追溯）。
    圖表標題的續行（02 mark_caption_variants 設定接續）只接在同型別的標題後面，不會和正文混成一筆。"""
    chains: list[list[TextBlock]] = []
    tail: dict[str, list[TextBlock]] = {}
    for b in sorted(doc.blocks, key=lambda x: x.order):
        if reasons[b.block_id] is not None:
            continue
        chain = None
        if b.block_type in CONTINUABLE:
            chain = tail.pop(b.continues_from_block_id, None)
        elif b.block_type in CAPTION_TYPES and b.continues_from_block_id in tail \
                and tail[b.continues_from_block_id][-1].block_type == b.block_type:
            chain = tail.pop(b.continues_from_block_id)
        if chain is None:
            chain = []
            chains.append(chain)
        chain.append(b)
        tail[b.block_id] = chain
    return chains


def dangling_continuations(records: list[dict]) -> list[dict]:
    """續段的 continues_from_block_id 指向沒有匯出的區塊（鏈頭被排除，例如待確認）→ 這筆只剩半句話。
    應為 0；不是 0 時要回頭查 01／02 為什麼鏈頭不能用（2026-10-15 TH014 p.102、AR012 p.11）。"""
    exported = {i for r in records for i in r["block_ids"]}
    return [r for r in records if r["continues_from_block_id"] and r["continues_from_block_id"] not in exported]


def data_status(b: TextBlock, by_id: dict[str, TextBlock], reasons: dict) -> Optional[str]:
    """圖說／表格標題：連結的圖表內容有沒有被擷取、有沒有收錄進 RAG。其他區塊回傳 None。"""
    if b.block_type not in ("figure_caption", "table_caption"):
        return None
    linked = [by_id[i] for i in b.linked_block_ids if i in by_id]
    tables = [x for x in linked if x.block_type == "table" and x.parse_status == "ok"]
    if any(reasons.get(x.block_id) is None for x in tables):
        return "extracted"
    if tables:
        return "extracted_pending_review"
    if any(x.block_type in ("image_region", "chart_region", "table") for x in linked):
        return "not_extracted"
    return "unknown"


def to_record(chain: list[TextBlock], src: SourceInfo, by_id: dict[str, TextBlock], reasons: dict) -> dict:
    head = chain[0]
    pages = sorted({b.page_number for b in chain if b.page_number})
    search, display = "", ""
    cited: list[str] = []
    for b in chain:
        search, display = _join(search, b.search_text), _join(display, b.cleaned_text)
        cited += [c for c in b.cited_works if c not in cited]
    status = data_status(head, by_id, reasons)
    return {
        "doc_id": head.doc_id,
        "block_id": head.block_id,
        "block_ids": [b.block_id for b in chain],   # 合併前的原始區塊，可逐段追溯
        "block_type": head.block_type,
        "text": search,                             # 給搜尋／embedding
        "display_text": display,                    # 回答時引用原文
        "pages": pages,
        "section": head.section,
        "continues_from_block_id": head.continues_from_block_id,
        "citation": short_citation(src, pages),
        "reference": reference_string(src),
        # 非空 = 段落含轉述他人研究的句子，回答時要寫成「Chen (2024) 引用 Zhang (2019)」而非 Chen 的結論
        "cited_works": cited,
        # 沒有引用標記不代表一定是本研究的發現，只代表沒有偵測到標記
        "attribution": "cites_other_work" if cited else "no_citation_marker",
        "data_status": status,
        "data_note": DATA_STATUS_NOTE.get(status),
        "linked_block_ids": head.linked_block_ids if status else [],
    }


def attach_practical_activities(records: list[dict], activities) -> None:
    """步驟與正文重複的實作活動，附在正文那筆紀錄的 corroborating_units（只附原文定位與全文，不併進搜尋文字）。

    同一方法只灌一次（text／display_text 不變），但第二個原文出處可追溯：教材 p.26 粉底表 ↔ p.28–29 Activity 1。
    沒有對到的紀錄不加這個欄位，其他紀錄的輸出完全不變。
    """
    owner = {i: r for r in records for i in r["block_ids"]}
    for act in activities:
        if act.matched_block_id is None or act.matched_block_id not in owner:
            continue
        owner[act.matched_block_id].setdefault("corroborating_units", []).append({
            "kind": "practical_activity",
            "relation": "repeats_steps_of",
            "title": act.title,
            "block_ids": list(act.block_ids),
            "step_block_ids": list(act.step_ids),
            "pages": list(act.pages),
            "steps_matched": f"{act.step_match[0]}/{act.step_match[1]}",
            "display_text": act.text,
        })


def export(doc: Document, src: SourceInfo, out_dir: Path) -> tuple[Path, Counter]:
    reasons = classify_blocks(doc)
    by_id = {b.block_id: b for b in doc.blocks}
    chains = build_chains(doc, reasons)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{doc.manifest.doc_id}.jsonl"
    records = [to_record(c, src, by_id, reasons) for c in chains]
    attach_practical_activities(records, practical_activities(doc, reasons))
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    stats = Counter(r or "eligible" for r in reasons.values())
    citing = sum(1 for r in records if r["cited_works"])
    merged = sum(1 for c in chains if len(c) > 1)
    caption_only = sum(1 for r in records if r["data_status"] in ("not_extracted", "unknown", "extracted_pending_review"))
    dangling = dangling_continuations(records)
    lines = [
        f"# RAG 可用段落：{doc.manifest.doc_id}",
        "",
        f"- 出處：{reference_string(src)}",
        f"- 書目查證：{src.verified_from}",
        f"- 可用：**{stats['eligible']}** / {len(doc.blocks)} 個區塊 → 匯出 **{len(records)}** 筆"
        f"（其中 {merged} 筆由被分頁／換欄切斷的段落合併）",
        f"- 含引用他人研究（cited_works 非空）：{citing} 筆；沒有引用標記不等於本研究結論",
        f"- 圖說／表格標題但圖表數據未收錄（未擷取或待確認）：{caption_only} 筆",
        f"- 續段的前一段沒有匯出（只剩半句，應為 0）：{len(dangling)} 筆"
        + "".join(f"\n  - {r['block_id']} ← {r['continues_from_block_id']}" for r in dangling),
        "",
        "| 排除原因 | 數量 |",
        "|---|---|",
        *(f"| {REASON_LABEL[k]} | {stats[k]} |" for k in REASON_LABEL if stats[k]),
        "",
    ]
    (out_dir / f"{doc.manifest.doc_id}.summary.md").write_text("\n".join(lines), encoding="utf-8")
    return path, stats


def iter_inputs(paths: Iterable[Path]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        out += sorted(p.glob("*.json")) if p.is_dir() else [p]
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="匯出可進 RAG 的段落（附出處）")
    ap.add_argument("inputs", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    args = ap.parse_args(argv)
    sources = load_sources(args.sources)
    for p in iter_inputs(args.inputs):
        doc = Document.load(p)
        if doc.stage != "cleaned":
            raise ValueError(f"{p}：只接受 02_clean 輸出（stage=cleaned）")
        src = sources.get(doc.manifest.doc_id)
        if src is None:
            raise KeyError(f"{doc.manifest.doc_id} 不在 {args.sources}：請先登錄書目資訊，沒有出處的段落不能進 RAG")
        path, stats = export(doc, src, args.out)
        excluded = ", ".join(f"{k}={v}" for k, v in sorted(stats.items()) if k != "eligible")
        records = [json.loads(line) for line in open(path, encoding="utf-8")]
        print(f"[rag_ready] {doc.manifest.doc_id}: eligible={stats['eligible']}/{sum(stats.values())} "
              f"→ {len(records)} records → {path}")
        print(f"            excluded: {excluded}")
        dangling = dangling_continuations(records)
        if dangling:
            print(f"            警告：{len(dangling)} 筆續段的前一段沒有匯出（只剩半句）："
                  + ", ".join(r["block_id"] for r in dangling))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
