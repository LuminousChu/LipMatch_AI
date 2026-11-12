"""S3 資料接入與人工審核指令。

  python -m app.ingest inventory --root F:/LipMatch_AI                 # 盤點，不連資料庫
  python -m app.ingest import --root F:/LipMatch_AI [--doc TH013]  # 匯入成 draft 版本（冪等）
  python -m app.ingest import --root … --units <A 的 support_units.jsonl>  # 修復 D：連同支持單位匯入
  python -m app.ingest verify-units --units <A 的 support_units.jsonl>      # 修復 D：唯讀核對與資料庫逐字一致
  python -m app.ingest status                                          # 各文件版本與審核數量
  python -m app.ingest review-list --doc TH013 [--version v1] [--status pending] [--out x.csv]
  python -m app.ingest review --reviewer ops --decisions x.csv         # 依人工填寫的決定逐筆審核
  python -m app.ingest review --reviewer ops --doc TH013 --chunk-key TH013_p0004_b002 \
                              --decision approve --reason "已對照原文 p.4"
  python -m app.ingest publish --doc TH013 --version v1
  python -m app.ingest set-visibility --doc TH013 --visibility all

規則：
- 只讀 --root 底下的 config/ 與 data/processed/；doc_id 只接受登錄清單中的英數 ID，路徑正規化後必須仍在 root 內。
- 匯入的片段一律 pending；ETL 的 needs_review=false 不代表人工核准。
- 同內容重複匯入不建新版本；內容不同建新的 draft 版本，不覆寫既有版本。
- 審核需管理者帳號與密碼、每筆都要有決定與理由；寫入 chunk_review_events。
- 審核清單含原文，預設寫到 data/review/s3/（不進 git）。
"""
from __future__ import annotations

import argparse
import csv
import getpass
import hashlib
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .security import verify_password

DOC_ID = re.compile(r"^[A-Za-z0-9_-]+$")
# 測試資料，永遠不匯入（規範 7）
EXCLUDED_DOC_IDS = {"sample_makeup_article"}

# 需要人工特別留意的片段（只是提示，不會自動核准或排除）
_BOILERPLATE = re.compile(
    r"(?i)(copyright|©|all rights reserved|creative commons|\bCC[ -]BY|licen[cs]e[ds]?\b|open access|"
    r"keywords?\s*[:：]|關鍵字|關鍵詞|授權|版權)"
)
_INSTRUCTION = re.compile(
    r"(?i)(ignore\s+(all\s+|the\s+)?(previous|above|prior)|disregard\s+(all|the|previous)|system\s+prompt|"
    r"you\s+are\s+(now\s+)?(an?|the)\s+(ai|assistant|model)|忽略(以上|前面|先前|之前)|系統提示|你現在是)"
)
# 開頭像是接在別的句子後面：英文小寫或標點開頭；中文「兩個字＋逗號」但不是常見連接詞（如「部分，其結果…」）
_MID_START = re.compile(r"^(?:[a-z]|[，、。；：）)\],.;:])")
_CJK_LEAD = re.compile(r"^([\u4e00-\u9fff]{1,2})[，、]")
_CJK_CONNECTORS = {"因此", "此外", "然而", "另外", "其中", "同時", "最後", "首先", "其次", "因為", "所以",
                   "但是", "例如", "總之", "此時", "接著", "然後", "另一", "目前", "本研究", "結果"}
FLAG_LABELS = {
    "cross_page_merge": "跨頁／換欄合併：須合讀確認接續正確",
    "continues_from_excluded": "上一段被排除：可能斷句，須對照原文前文",
    "caption_without_data": "圖說／表格標題：圖表數據未收錄",
    "caption_with_data": "圖說／表格標題：表格數據已另行收錄",
    "cites_other_work": "含轉引他人研究：不可寫成本研究發現",
    "possible_boilerplate": "疑似授權聲明／關鍵字／版權文字",
    "possible_instruction": "疑似指令文字：只能當資料，須人工判斷",
    "short_text": "文字很短：可能是標籤或殘段",
    "starts_mid_sentence": "開頭疑似接在其他句子後：可能缺前文",
}
REVIEW_COLUMNS = [
    "doc_key", "version_label", "chunk_key", "pages", "html_locator", "section", "review_status", "flags",
    "prior_decision", "content_sha256", "quote_text", "decision", "reason",
]
DECISIONS = {"approve": "approve", "核准": "approve", "通過": "approve",
             "reject": "reject", "退回": "reject", "不通過": "reject"}


class IngestError(RuntimeError):
    pass


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _inside(root: Path, rel: str | Path) -> Path:
    """把相對路徑接到 root 底下，正規化後必須仍在 root 內（擋 ../ 與絕對路徑）。"""
    path = (root / rel).resolve()
    if not path.is_relative_to(root):
        raise IngestError(f"路徑超出資料根目錄：{rel}")
    return path


# ---------------------------------------------------------------------------
# 讀取 ETL 輸出
# ---------------------------------------------------------------------------
@dataclass
class ChunkIn:
    chunk_key: str
    ordinal: int
    section: str | None
    page_start: int | None
    page_end: int | None
    quote_text: str
    search_text: str
    attributes: dict
    content_sha256: str
    html_locator: str | None = None


@dataclass
class DocIn:
    doc_key: str
    title: str
    doc_type: str
    bibliography: dict
    source_sha256: str
    source_filename: str
    etl_version: str
    raw_verified: bool
    chunks: list[ChunkIn]
    import_sha256: str
    flag_counts: Counter = field(default_factory=Counter)


def load_registry(root: Path) -> tuple[list[dict], dict[str, dict]]:
    docs = json.loads(_inside(root, "config/documents.json").read_text(encoding="utf-8"))["documents"]
    sources = json.loads(_inside(root, "config/sources.json").read_text(encoding="utf-8"))
    return docs, {k: v for k, v in sources.items() if not k.startswith("_")}


def _flags(rec: dict, all_block_ids: set[str]) -> list[str]:
    text = rec["display_text"]
    flags = []
    if len(rec.get("block_ids") or []) > 1 or len(rec.get("pages") or []) > 1:
        flags.append("cross_page_merge")
    cont = rec.get("continues_from_block_id")
    if cont and cont not in all_block_ids:
        flags.append("continues_from_excluded")
    if rec.get("data_status"):
        flags.append("caption_without_data" if rec["data_status"] != "extracted" else "caption_with_data")
    if rec.get("cited_works"):
        flags.append("cites_other_work")
    if _BOILERPLATE.search(text):
        flags.append("possible_boilerplate")
    if _INSTRUCTION.search(text):
        flags.append("possible_instruction")
    if len(text.strip()) < 40:
        flags.append("short_text")
    lead = _CJK_LEAD.match(text.strip())
    if _MID_START.match(text.strip()) or (lead and lead.group(1) not in _CJK_CONNECTORS):
        flags.append("starts_mid_sentence")
    return flags


def _html_block_locators(blocks: list[dict]) -> dict[str, dict]:
    """網頁 cleaned 區塊 → {block_id: {locator: DOM 路徑, heading_path: 所在的標題階層}}。

    標題階層依文件順序累積：遇到 h 層級 n 的標題就丟掉層級 ≥ n 的舊標題；被清洗移除的標題不算。"""
    out: dict[str, dict] = {}
    stack: list[tuple[int, str]] = []
    for b in blocks:
        level, text = b.get("heading_level"), (b.get("cleaned_text") or "").strip()
        if b.get("block_type") == "heading" and isinstance(level, int) and text and not b.get("is_removed"):
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, text))
        out[b["block_id"]] = {"locator": b.get("source_locator"), "heading_path": [t for _, t in stack]}
    return out


# ---------------------------------------------------------------------------
# 修復 D：A 交付的支持單位（contract-v0 support_unit）
# ---------------------------------------------------------------------------
UNIT_FIELDS = ("unit_id", "source_id", "source_version", "chunk_id", "text", "text_sha256", "search_text",
               "search_text_sha256", "locator_primary", "locator_secondary", "section_original", "section_search",
               "content_role", "support_scope", "conditions", "links", "product_mentions", "eligibility")
# 契約欄位中存進 chunks.attributes.support_unit 的部分（text／search_text 本身存在 quote_text／search_text 欄）；
# contract-v1 允許的其他欄位（export_sha256、search_text_base_sha256、tokens_cl100k、citation、rules…）也一併原樣保存
UNIT_ATTRS = tuple(f for f in UNIT_FIELDS if f not in ("text", "search_text", "source_id", "chunk_id"))
_NOT_IN_ATTRS = ("text", "search_text", "source_id", "chunk_id")
LINK_KEYS = ("prev", "next", "parent", "corroborating_units")


def _sha_text(text: str) -> str:
    return _sha256(text.encode("utf-8"))


def load_support_units(jsonl: Path) -> tuple[dict[str, list[dict]], dict]:
    """讀 A 的支持單位 JSONL 與同目錄 manifest.json，核對後回傳（{source_id: [單位…]}, manifest 摘要）。

    manifest.json：{"files": [{"path": 相對 manifest 的路徑, "sha256", "lines"}], "generator_commit",
    "input_export_sha256"}。檔案 SHA-256、行數、每列 text／search_text 的 SHA-256 任何一項不符就整份拒絕。
    eligibility 只原樣保存：匯入的片段一律 pending，A 的標記不等於人工核准。"""
    manifest_path = jsonl.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = next((f for f in manifest.get("files") or []
                  if (manifest_path.parent / f.get("path", "")).resolve() == jsonl.resolve()), None)
    if entry is None:
        raise IngestError(f"manifest.json 沒有登錄 {jsonl.name}")
    if Path(entry["path"]).is_absolute() or ".." in Path(entry["path"]).parts:
        raise IngestError("manifest.json 的 path 必須是 manifest 所在目錄底下的相對路徑")
    data = jsonl.read_bytes()
    if _sha256(data) != entry.get("sha256"):
        raise IngestError(f"{jsonl.name} 的 SHA-256 與 manifest 不符")
    lines = [ln for ln in data.decode("utf-8").splitlines() if ln.strip()]
    if len(lines) != entry.get("lines"):
        raise IngestError(f"{jsonl.name} 行數 {len(lines)} 與 manifest 的 {entry.get('lines')} 不符")
    by_doc: dict[str, list[dict]] = {}
    seen: set[str] = set()
    for n, line in enumerate(lines, start=1):
        u = json.loads(line)
        missing = [f for f in UNIT_FIELDS if f not in u]
        if missing:
            raise IngestError(f"支持單位第 {n} 行缺欄位：{missing}")
        uid, cid = u["unit_id"], u["chunk_id"]
        if uid in seen:
            raise IngestError(f"支持單位重複：{uid}")
        seen.add(uid)
        merge = u.get("merge_range")
        if merge is not None:
            members = merge.get("members") if isinstance(merge, dict) else None
            if (uid != f"{cid}#m" or merge.get("main") != cid or not isinstance(members, list) or len(members) < 2
                    or cid not in members or len(set(members)) != len(members)):
                raise IngestError(f"{uid}：合併單位必須是 <主段>#m，merge_range.main＝chunk_id，members 含主段且至少兩段")
        elif not (uid == cid or re.fullmatch(re.escape(cid) + r"#[rsa]\d+", uid)):
            raise IngestError(f"{uid}：unit_id 必須等於 chunk_id，或是 <chunk_id>#r<n>／#s<n>／#a<n>／#m")
        retrieval = u.get("retrieval")
        if retrieval is not None and (not isinstance(retrieval, dict) or not isinstance(retrieval.get("exclude"), bool)):
            raise IngestError(f"{uid}：retrieval 必須是 {{exclude: bool}}")
        if not isinstance(u["text"], str) or not u["text"].strip() or not isinstance(u["search_text"], str):
            raise IngestError(f"{uid}：text／search_text 必須是非空字串")
        if _sha_text(u["text"]) != u["text_sha256"] or _sha_text(u["search_text"]) != u["search_text_sha256"]:
            raise IngestError(f"{uid}：text 或 search_text 與其 SHA-256 不符")
        if not isinstance(u["conditions"], list) or not isinstance(u["product_mentions"], list):
            raise IngestError(f"{uid}：conditions／product_mentions 必須是陣列")
        links = u["links"]
        # A 第一批：prev／next／parent 為單一 ID 或 null；corroborating_units 為陣列（'<chunk>#c<n>'）
        if not isinstance(links, dict) or set(links) - set(LINK_KEYS)                 or not isinstance(links.get("corroborating_units", []), list)                 or not all(links.get(k) is None or isinstance(links[k], (str, list)) for k in ("prev", "next", "parent")):
            raise IngestError(f"{uid}：links 只能有 {LINK_KEYS}；prev／next／parent 為 ID、陣列或 null，"
                              "corroborating_units 為陣列")
        by_doc.setdefault(u["source_id"], []).append(u)
    summary = {"jsonl": jsonl.name, "sha256": entry["sha256"], "lines": entry["lines"],
               "generator_commit": manifest.get("generator_commit"),
               "input_export_sha256": manifest.get("input_export_sha256")}
    return by_doc, summary


def _make_chunk(chunk_key: str, section: str | None, page_start: int | None, page_end: int | None, quote: str,
                search: str, attributes: dict, html_locator: str | None, is_html: bool) -> ChunkIn:
    """組片段並算內容指紋（與 load_document 同規則：PDF 不含 html_locator）。"""
    content = {"chunk_key": chunk_key, "section": section,
               "pages": sorted({p for p in (page_start, page_end) if p is not None}),
               "quote_text": quote, "search_text": search, "attributes": attributes}
    if is_html:
        content["html_locator"] = html_locator
    return ChunkIn(chunk_key=chunk_key, ordinal=0, section=section, page_start=page_start, page_end=page_end,
                   quote_text=quote, search_text=search, attributes=attributes,
                   content_sha256=_sha256(_canonical(content)), html_locator=html_locator)


def _citation_only(c: ChunkIn, is_html: bool, **extra) -> ChunkIn:
    """原片段改成只供引用（attributes.citation_only：不建向量、不進檢索），另記為什麼。"""
    return _make_chunk(c.chunk_key, c.section, c.page_start, c.page_end, c.quote_text, c.search_text,
                       {**c.attributes, "citation_only": True, **extra}, c.html_locator, is_html)


def _apply_units(doc_id: str, chunks: list[ChunkIn], units: list[dict], is_html: bool) -> list[ChunkIn]:
    """把支持單位套到 ETL 片段上：整段單位 → 換搜尋文字並附 support_unit；子範圍單位（#r、#s）→ 各自成為片段，
    母片段保留供引用（attributes.citation_only，不建向量、不進檢索）。沒有單位的片段原樣保留。
    切片整修 F 版：
    - #m 合併單位 → 新片段（原文＝成員原文以兩個換行相接，頁碼與 block_ids 合併），接在最後一個成員後面；
      成員片段保留供引用（citation_only＋merged_into）
    - 整段單位 retrieval.exclude → 該片段只供引用（citation_only＋retrieval_exclusion），例如參考文獻列表
    每個單位都必須對得上本文件的片段與原文；整段單位的 unit_id 必須等於 chunk_key；一個片段只能屬於一種單位。"""
    by_chunk: dict[str, list[dict]] = {}
    merged: list[dict] = []
    keys = {c.chunk_key for c in chunks}
    for u in units:
        if u["source_id"] != doc_id or u["chunk_id"] not in keys:
            raise IngestError(f"{doc_id}：支持單位 {u['unit_id']} 對不上本文件的片段")
        if "merge_range" in u:
            merged.append(u)
        else:
            by_chunk.setdefault(u["chunk_id"], []).append(u)
    index = {c.chunk_key: i for i, c in enumerate(chunks)}
    member_of: dict[str, dict] = {}
    for u in merged:
        members = u["merge_range"]["members"]
        if any(m not in index for m in members):
            raise IngestError(f"{doc_id}：合併單位 {u['unit_id']} 的成員對不上本文件的片段")
        pos = [index[m] for m in members]
        if pos != list(range(pos[0], pos[0] + len(pos))):
            raise IngestError(f"{doc_id}：合併單位 {u['unit_id']} 的成員不是緊鄰片段")
        if "\n\n".join(chunks[i].quote_text for i in pos) != u["text"]:
            raise IngestError(f"{doc_id}：合併單位 {u['unit_id']} 的原文不等於成員原文以兩個換行相接（版本不一致）")
        for m in members:
            if m in member_of or m in by_chunk:
                raise IngestError(f"{doc_id}：{m} 同時屬於合併單位 {u['unit_id']} 與其他單位")
            member_of[m] = u
    out: list[ChunkIn] = []
    for c in chunks:
        if c.chunk_key in member_of:
            u = member_of[c.chunk_key]
            members = u["merge_range"]["members"]
            out.append(_citation_only(c, is_html, merged_into=u["unit_id"]))
            if c.chunk_key == members[-1]:
                mem = [chunks[index[m]] for m in members]
                main = chunks[index[u["chunk_id"]]]
                pages = [p for x in mem for p in (x.page_start, x.page_end) if p is not None]
                attributes = {**main.attributes, "block_ids": [b for x in mem for b in x.attributes["block_ids"]],
                              "merged_from": members,
                              "support_unit": {k: v for k, v in u.items() if k not in _NOT_IN_ATTRS}}
                if is_html:
                    attributes["html_locators"] = [loc for x in mem for loc in x.attributes["html_locators"]]
                out.append(_make_chunk(u["unit_id"], main.section, min(pages) if pages else None,
                                       max(pages) if pages else None, u["text"], u["search_text"], attributes,
                                       main.html_locator, is_html))
            continue
        group = by_chunk.get(c.chunk_key)
        if not group:
            out.append(c)
            continue
        whole = [u for u in group if u["unit_id"] == c.chunk_key]
        if whole and len(group) > 1:
            raise IngestError(f"{doc_id}：{c.chunk_key} 同時有整段單位與子範圍單位")
        if not whole:   # contract-v1 multi_scenario_split（#r）／F 版長段切（#s）／已核准範圍（#a）：母段保留供引用
            out.append(_citation_only(c, is_html, split_into=[u["unit_id"] for u in group]))
        for u in group:
            if whole and u["text"] != c.quote_text:
                raise IngestError(f"{doc_id}：{u['unit_id']} 的原文與 rag_ready display_text 不同（版本不一致）")
            if not whole and u["text"] not in c.quote_text:
                raise IngestError(f"{doc_id}：{u['unit_id']} 的原文不是母片段原文的連續子字串")
            attributes = {**c.attributes, "support_unit": {k: v for k, v in u.items() if k not in _NOT_IN_ATTRS}}
            if not whole:
                attributes["parent_chunk_key"] = c.chunk_key
            if (u.get("retrieval") or {}).get("exclude"):
                attributes.update(citation_only=True, retrieval_exclusion=u["retrieval"])
            out.append(_make_chunk(u["unit_id"], c.section, c.page_start, c.page_end, u["text"], u["search_text"],
                                   attributes, c.html_locator, is_html))
    for i, c in enumerate(out):
        c.ordinal = i
    return out
# 核心邏輯：
# 1. 原文以資料庫既有的 rag_ready display_text 為準；A 的 text 必須等於它（整段）、是它的連續子字串（子範圍），
#    或是緊鄰幾段以兩個換行相接（合併）；不一致代表 A 用的匯出版本和這次匯入的不同，整份拒絕，不默默混用。
# 2. 條件、第二定位、連接關係、商品指涉、內容角色等放在 attributes.support_unit（進內容指紋）：
#    任何一項變了都會成為新的 draft 版本、片段重新待審，不覆寫已發布版本。
# 3. 不進檢索一律用 citation_only 表達（retrieval._INDEXABLE、release 都只認這個），片段仍可被引用與追溯。


def verify_units(conn: psycopg.Connection, by_doc: dict[str, list[dict]], label: str | None = None) -> dict:
    """唯讀核對：資料庫（每份文件指定版本或最新版本）的片段是否與 A 的支持單位逐字一致。

    比 quote_text 的 SHA＝text_sha256、search_text 的 SHA＝search_text_sha256（xVITA 快取以此為鍵，需全部相同才命中）、
    attributes.support_unit 的契約欄位與單位相同；另列只供引用的母段。連線須使用 dict_row。"""
    report: dict[str, Any] = {"units": 0, "matched": 0, "missing": [], "text_mismatch": [], "search_text_mismatch": [],
                              "field_mismatch": [], "citation_only_parents": 0, "versions": {}}
    for doc_key, units in sorted(by_doc.items()):
        try:
            v = _version(conn, doc_key, label)
        except IngestError:
            report["missing"] += [u["unit_id"] for u in units]
            report["units"] += len(units)
            continue
        report["versions"][doc_key] = v["version_label"]
        rows = {r["chunk_key"]: r for r in conn.execute(
            "SELECT chunk_key, quote_text, search_text, attributes FROM chunks WHERE document_version_id = %s",
            (v["id"],))}
        report["citation_only_parents"] += sum(bool(r["attributes"].get("citation_only")) for r in rows.values())
        for u in units:
            report["units"] += 1
            r = rows.get(u["unit_id"])
            if r is None:
                report["missing"].append(u["unit_id"])
            elif _sha_text(r["quote_text"]) != u["text_sha256"]:
                report["text_mismatch"].append(u["unit_id"])
            elif _sha_text(r["search_text"]) != u["search_text_sha256"]:
                report["search_text_mismatch"].append(u["unit_id"])
            elif any((r["attributes"].get("support_unit") or {}).get(f) != u[f] for f in UNIT_ATTRS):
                report["field_mismatch"].append(u["unit_id"])
            elif bool(r["attributes"].get("citation_only")) != bool((u.get("retrieval") or {}).get("exclude")):
                report["field_mismatch"].append(u["unit_id"])           # 排除檢索的單位必須只供引用，其他不可
            elif "merge_range" in u and any((rows.get(m) or {}).get("attributes", {}).get("merged_into") != u["unit_id"]
                                            for m in u["merge_range"]["members"]):
                report["field_mismatch"].append(u["unit_id"])           # 合併成員必須在、且標明併進這個單位
            else:
                report["matched"] += 1
    report["ok"] = report["matched"] == report["units"]
    return report
# 核心邏輯：只讀不寫；任何一類不符都列出 unit_id，E 據此判斷快取能否全部命中、欄位是否帶進資料庫。


def load_document(root: Path, doc_id: str, sources: dict[str, dict],
                  units: list[dict] | None = None) -> DocIn:
    if not DOC_ID.match(doc_id) or doc_id in EXCLUDED_DOC_IDS:
        raise IngestError(f"{doc_id}：不可匯入（非法 ID 或測試資料）")
    if doc_id not in sources:
        raise IngestError(f"{doc_id}：config/sources.json 沒有書目，沒有出處的片段不能匯入")
    cleaned = json.loads(_inside(root, f"data/processed/cleaned/{doc_id}.json").read_text(encoding="utf-8"))
    if cleaned.get("stage") != "cleaned":
        raise IngestError(f"{doc_id}：cleaned 檔 stage 不是 cleaned")
    manifest = cleaned["manifest"]
    if manifest["doc_id"] != doc_id:
        raise IngestError(f"{doc_id}：cleaned manifest 的 doc_id 不符")

    # 原始檔在這台電腦時，核對指紋；不符代表 ETL 輸出已過期
    raw_path = _inside(root, manifest["source_path"])
    raw_verified = False
    if raw_path.is_file():
        if _sha256(raw_path.read_bytes()) != manifest["sha256"]:
            raise IngestError(f"{doc_id}：原始檔與 cleaned 記錄的 SHA-256 不符，請先重跑 ETL")
        raw_verified = True

    jsonl = _inside(root, f"data/processed/rag_ready/{doc_id}.jsonl")
    records = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not records:
        raise IngestError(f"{doc_id}：rag_ready 沒有任何片段")
    keys = [r["block_id"] for r in records]
    if len(keys) != len(set(keys)):
        raise IngestError(f"{doc_id}：rag_ready 有重複的 block_id")
    all_block_ids = {b for r in records for b in (r.get("block_ids") or [r["block_id"]])}
    owner = {b: r["block_id"] for r in records for b in (r.get("block_ids") or [r["block_id"]])}
    is_html = manifest.get("source_type") == "html"
    if is_html:
        source_url = manifest.get("source_url") or sources[doc_id].get("url")
        if not source_url:
            raise IngestError(f"{doc_id}：網頁來源沒有網址（manifest.source_url 與書目 url 都空白），無法追溯")
        html_blocks = _html_block_locators(cleaned["blocks"])

    chunks: list[ChunkIn] = []
    counts: Counter = Counter()
    for ordinal, rec in enumerate(records):
        if rec.get("doc_id") != doc_id:
            raise IngestError(f"{doc_id}：{rec.get('block_id')} 的 doc_id 不符")
        quote = (rec.get("display_text") or "").strip()
        if not quote:
            raise IngestError(f"{doc_id}：{rec['block_id']} 沒有 display_text")
        pages = sorted(p for p in (rec.get("pages") or []) if isinstance(p, int))
        flags = _flags(rec, all_block_ids)
        counts.update(flags)
        cont = rec.get("continues_from_block_id")
        attributes = {
            "block_ids": rec.get("block_ids") or [rec["block_id"]],
            "block_type": rec.get("block_type"),
            "continues_from_block_id": cont,
            "continues_from_chunk_key": owner.get(cont) if cont else None,
            "linked_block_ids": rec.get("linked_block_ids") or [],
            "cited_works": rec.get("cited_works") or [],
            "attribution": rec.get("attribution"),
            "data_status": rec.get("data_status"),
            "data_note": rec.get("data_note"),
            "citation": rec.get("citation"),
            "review_flags": flags,
        }
        page_start, page_end = (pages[0], pages[-1]) if pages else (None, None)
        html_locator = None
        if is_html:
            # 網頁沒有頁碼：定位用 DOM 路徑（對應這一版原始檔的 SHA-256）＋網址＋標題階層，不虛構頁碼
            if pages:
                raise IngestError(f"{doc_id}：{rec['block_id']} 是網頁片段卻帶頁碼")
            located = [html_blocks.get(b) for b in attributes["block_ids"]]
            if any(loc is None or not loc["locator"] for loc in located):
                raise IngestError(f"{doc_id}：{rec['block_id']} 在 cleaned 找不到網頁段落定位")
            html_locator = located[0]["locator"]
            attributes.update(source_url=source_url, html_locators=[loc["locator"] for loc in located],
                              heading_path=located[0]["heading_path"])
        content = {"chunk_key": rec["block_id"], "section": rec.get("section"), "pages": pages,
                   "quote_text": quote, "search_text": rec.get("text") or quote, "attributes": attributes}
        if is_html:  # PDF 的指紋內容不變（既有版本重匯仍為 unchanged）
            content["html_locator"] = html_locator
        chunks.append(ChunkIn(
            chunk_key=rec["block_id"], ordinal=ordinal, section=rec.get("section"),
            page_start=page_start, page_end=page_end, quote_text=quote,
            search_text=rec.get("text") or quote, attributes=attributes,
            content_sha256=_sha256(_canonical(content)), html_locator=html_locator,
        ))
    if not is_html and any(c.page_start is None for c in chunks):
        raise IngestError(f"{doc_id}：有片段沒有頁碼（PDF 來源必須有頁碼）")
    if units:
        chunks = _apply_units(doc_id, chunks, units, is_html)

    bibliography = sources[doc_id]
    # 指紋只看內容：來源檔、書目與片段；ETL 重跑但輸出相同時不建新版本
    import_sha256 = _sha256(_canonical({
        "source_sha256": manifest["sha256"], "bibliography": bibliography,
        "chunks": [c.content_sha256 for c in chunks],
    }))
    doc_type = "web_article" if "web" in (bibliography.get("doc_type") or "").lower() else "academic_paper"
    return DocIn(
        doc_key=doc_id, title=bibliography["title"], doc_type=doc_type, bibliography=bibliography,
        source_sha256=manifest["sha256"], source_filename=Path(manifest["source_path"]).name,
        etl_version=f"{manifest.get('extractor')} @ {manifest.get('extracted_at')} (schema {cleaned.get('schema_version')})",
        raw_verified=raw_verified, chunks=chunks, import_sha256=import_sha256, flag_counts=counts,
    )


# ---------------------------------------------------------------------------
# 盤點（不連資料庫）
# ---------------------------------------------------------------------------
def inventory(root: Path) -> dict:
    docs, sources = load_registry(root)
    registered = {d["doc_id"] for d in docs}
    report: dict[str, Any] = {"documents": [], "unregistered_raw": [], "excluded_present": []}
    for d in docs:
        doc_id = d["doc_id"]
        row: dict[str, Any] = {"doc_id": doc_id, "has_bibliography": doc_id in sources}
        try:
            doc = load_document(root, doc_id, sources)
        except (IngestError, FileNotFoundError, KeyError, json.JSONDecodeError) as exc:
            row["error"] = str(exc)
        else:
            row.update(chunks=len(doc.chunks), raw_verified=doc.raw_verified,
                       pages=(f"{min(c.page_start for c in doc.chunks)}–{max(c.page_end for c in doc.chunks)}"
                              if doc.chunks[0].page_start is not None else "網頁（DOM 定位）"),
                       flags=dict(doc.flag_counts), import_sha256=doc.import_sha256[:12])
        report["documents"].append(row)
    raw_dir = _inside(root, "data/raw")
    registered_paths = {_inside(root, d["path"]) for d in docs}
    if raw_dir.is_dir():
        for p in sorted(raw_dir.rglob("*")):
            if p.is_file() and p.suffix.lower() in (".pdf", ".html", ".htm") and p.resolve() not in registered_paths:
                report["unregistered_raw"].append(p.relative_to(root).as_posix())
    cleaned_dir = _inside(root, "data/processed/cleaned")
    if cleaned_dir.is_dir():
        report["excluded_present"] = sorted(
            p.stem for p in cleaned_dir.glob("*.json") if p.stem in EXCLUDED_DOC_IDS or p.stem not in registered
        )
    return report


# ---------------------------------------------------------------------------
# 匯入
# ---------------------------------------------------------------------------
def import_document(conn: psycopg.Connection, doc: DocIn) -> dict:
    """連線須使用 dict_row。一份文件一個交易。同內容 → unchanged；新內容 → 新的 draft 版本（片段全部 pending）。"""
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("ingest:" + doc.doc_key,))
        document_id = conn.execute(
            """INSERT INTO documents (doc_key, title, doc_type, rag_eligible, bibliography)
               VALUES (%s, %s, %s, TRUE, %s)
               ON CONFLICT (doc_key) DO UPDATE SET title = EXCLUDED.title, bibliography = EXCLUDED.bibliography
               RETURNING id""",
            (doc.doc_key, doc.title, doc.doc_type, Jsonb(doc.bibliography)),
        ).fetchone()["id"]
        existing = conn.execute(
            "SELECT version_label, status FROM document_versions WHERE document_id = %s AND import_sha256 = %s",
            (document_id, doc.import_sha256),
        ).fetchone()
        if existing:
            return {"doc_key": doc.doc_key, "result": "unchanged", "version_label": existing["version_label"],
                    "version_status": existing["status"], "chunks": len(doc.chunks)}
        n = conn.execute("SELECT count(*) AS n FROM document_versions WHERE document_id = %s",
                         (document_id,)).fetchone()["n"]
        label = f"v{n + 1}"
        version_id = conn.execute(
            """INSERT INTO document_versions (document_id, version_label, source_sha256, source_filename,
                                              etl_version, import_sha256)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
            (document_id, label, doc.source_sha256, doc.source_filename, doc.etl_version, doc.import_sha256),
        ).fetchone()["id"]
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO chunks (document_version_id, chunk_key, ordinal, section, page_start, page_end,
                                       html_locator, quote_text, search_text, content_sha256, attributes)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                [(version_id, c.chunk_key, c.ordinal, c.section, c.page_start, c.page_end, c.html_locator,
                  c.quote_text, c.search_text, c.content_sha256, Jsonb(c.attributes)) for c in doc.chunks],
            )
    return {"doc_key": doc.doc_key, "result": "created", "version_label": label,
            "version_status": "draft", "chunks": len(doc.chunks)}


# 一個文件版本在整份文件中的位置（審核頁版本提示用）。「最新」一律以 document_versions.id 最大者為準，
# 與 _version()（不指定版本時取最新）和匯入時 v{n+1} 的編號方式一致；不新增欄位、不需要遷移。
#   is_latest           這個版本是不是此文件最新匯入的版本
#   latest_label        此文件最新版本的標籤（不論狀態）
#   latest_draft_label  比目前發布版本更新的草稿中最新的一個（沒有發布版本時＝最新的草稿）；
#                       沒有這樣的草稿為 NULL。比發布版本舊、從未發布的草稿不算「最新草稿」，
#                       避免發布 v2 後畫面仍寫「最新草稿 v1」
#   published_label     此文件目前「已發布」的版本；沒有發布版本為 NULL（發布時舊版會被退役，最多一個）
_VERSION_INFO_SQL = """
    SELECT d.doc_key, d.visibility, d.is_active, dv.id AS version_id, dv.version_label, dv.status,
           dv.id = (SELECT max(x.id) FROM document_versions x WHERE x.document_id = d.id) AS is_latest,
           (SELECT x.version_label FROM document_versions x WHERE x.document_id = d.id
             ORDER BY x.id DESC LIMIT 1) AS latest_label,
           (SELECT x.version_label FROM document_versions x WHERE x.document_id = d.id AND x.status = 'draft'
               AND x.id > COALESCE((SELECT max(p.id) FROM document_versions p
                                     WHERE p.document_id = d.id AND p.status = 'published'), 0)
             ORDER BY x.id DESC LIMIT 1) AS latest_draft_label,
           (SELECT x.version_label FROM document_versions x WHERE x.document_id = d.id AND x.status = 'published'
             ORDER BY x.id DESC LIMIT 1) AS published_label
      FROM documents d JOIN document_versions dv ON dv.document_id = d.id"""


def status(conn: psycopg.Connection) -> list[dict]:
    """列出每份文件的每個版本：版本資訊（含是否最新、最新草稿、目前發布版本）＋各審核狀態的片段數。"""
    return conn.execute(
        f"""SELECT vi.*, cnt.chunks, cnt.pending, cnt.approved, cnt.rejected
              FROM ({_VERSION_INFO_SQL}) vi
              CROSS JOIN LATERAL (
                   SELECT count(*) AS chunks,
                          count(*) FILTER (WHERE c.review_status = 'pending') AS pending,
                          count(*) FILTER (WHERE c.review_status = 'approved') AS approved,
                          count(*) FILTER (WHERE c.review_status = 'rejected') AS rejected
                     FROM chunks c WHERE c.document_version_id = vi.version_id) cnt
             ORDER BY vi.doc_key, vi.version_id""",
    ).fetchall()


def version_info(conn: psycopg.Connection, doc_key: str, label: str | None) -> dict | None:
    """取一個文件版本的版本資訊（欄位同 status() 的版本部分）；label 為 None 時取最新版本；找不到回 None。"""
    if label:
        return conn.execute(
            f"{_VERSION_INFO_SQL} WHERE d.doc_key = %s AND dv.version_label = %s", (doc_key, label),
        ).fetchone()
    return conn.execute(
        f"{_VERSION_INFO_SQL} WHERE d.doc_key = %s ORDER BY dv.id DESC LIMIT 1", (doc_key,),
    ).fetchone()


def _version(conn: psycopg.Connection, doc_key: str, label: str | None) -> dict:
    if label:
        row = conn.execute(
            """SELECT dv.id, dv.version_label, dv.status FROM document_versions dv
                 JOIN documents d ON d.id = dv.document_id WHERE d.doc_key = %s AND dv.version_label = %s""",
            (doc_key, label),
        ).fetchone()
    else:
        row = conn.execute(
            """SELECT dv.id, dv.version_label, dv.status FROM document_versions dv
                 JOIN documents d ON d.id = dv.document_id WHERE d.doc_key = %s ORDER BY dv.id DESC LIMIT 1""",
            (doc_key,),
        ).fetchone()
    if row is None:
        raise IngestError(f"找不到版本：{doc_key} {label or '(最新)'}")
    return row


def review_rows(conn: psycopg.Connection, doc_key: str, label: str | None, statuses: Iterable[str]) -> list[dict]:
    v = _version(conn, doc_key, label)
    # prior_decision 只提示、不沿用。除了同指紋，也認「改編號前的同一段」：doc_key 不同，但去掉 doc_key
    # 前綴後片段編號、原文、檢索文字、章節、頁碼／網頁定位、屬性全部相同（2026-10-17 LEEDS_2024→TH013 等）。
    return conn.execute(
        """SELECT %(doc)s::text AS doc_key, %(label)s::text AS version_label, c.chunk_key,
                  CASE WHEN c.page_start = c.page_end THEN c.page_start::text
                       ELSE c.page_start || '-' || c.page_end END AS pages,
                  c.html_locator, c.section, c.review_status,
                  (SELECT string_agg(f, ' ') FROM jsonb_array_elements_text(c.attributes -> 'review_flags') f) AS flags,
                  (SELECT e.to_status || ' (' || CASE WHEN d2.doc_key = %(doc)s::text THEN dv2.version_label
                                                ELSE d2.doc_key || ' ' || dv2.version_label || '，改編號前同內容' END || ')'
                     FROM chunk_review_events e JOIN chunks c2 ON c2.id = e.chunk_id
                     JOIN document_versions dv2 ON dv2.id = c2.document_version_id
                     JOIN documents d2 ON d2.id = dv2.document_id
                    WHERE c2.id <> c.id
                      AND (c2.content_sha256 = c.content_sha256
                           OR (d2.doc_key <> %(doc)s::text
                               AND starts_with(c2.chunk_key, d2.doc_key || '_')
                               AND substr(c2.chunk_key, length(d2.doc_key) + 2)
                                   = substr(c.chunk_key, length(%(doc)s::text) + 2)
                               AND c2.quote_text = c.quote_text AND c2.search_text = c.search_text
                               AND c2.section IS NOT DISTINCT FROM c.section
                               AND c2.page_start IS NOT DISTINCT FROM c.page_start
                               AND c2.page_end IS NOT DISTINCT FROM c.page_end
                               AND c2.html_locator IS NOT DISTINCT FROM c.html_locator
                               AND replace(c2.attributes::text, d2.doc_key || '_', '')
                                   = replace(c.attributes::text, %(doc)s::text || '_', '')))
                    ORDER BY e.created_at DESC LIMIT 1) AS prior_decision,
                  c.content_sha256, c.quote_text, '' AS decision, '' AS reason
             FROM chunks c WHERE c.document_version_id = %(ver)s AND c.review_status = ANY(%(statuses)s)
            ORDER BY c.ordinal""",
        {"doc": doc_key, "label": v["version_label"], "ver": v["id"], "statuses": list(statuses)},
    ).fetchall()


def write_review_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig：Excel 開啟中文不亂碼
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=REVIEW_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r[k]) for k in REVIEW_COLUMNS})


@dataclass
class Decision:
    doc_key: str
    version_label: str | None
    chunk_key: str
    decision: str
    reason: str
    content_sha256: str | None


def read_decisions(path: Path) -> list[Decision]:
    """只取有填 decision 的列；任何一列格式錯誤就整份拒絕（不做部分套用）。"""
    out: list[Decision] = []
    errors: list[str] = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for i, row in enumerate(csv.DictReader(f), start=2):
            raw = (row.get("decision") or "").strip()
            if not raw:
                continue
            decision = DECISIONS.get(raw.lower(), DECISIONS.get(raw))
            reason = (row.get("reason") or "").strip()
            if decision is None:
                errors.append(f"第 {i} 列 decision 無法辨識：{raw}")
            elif len(reason) < 2:
                errors.append(f"第 {i} 列缺少理由")
            else:
                out.append(Decision(row["doc_key"], row.get("version_label") or None, row["chunk_key"],
                                    decision, reason, row.get("content_sha256") or None))
    if errors:
        raise IngestError("審核檔有錯，未套用任何一筆：\n" + "\n".join(errors))
    return out


def authenticate_admin(conn: psycopg.Connection, username: str, password: str) -> int:
    row = conn.execute(
        "SELECT id, password_hash, role, is_active FROM users WHERE username = %s", (username.strip().lower(),)
    ).fetchone()
    if row is None or not verify_password(password, row["password_hash"]) or not row["is_active"]:
        raise IngestError("審核人帳號或密碼錯誤")
    if row["role"] != "admin":
        raise IngestError("審核人必須是管理者")
    return row["id"]


def apply_decisions(conn: psycopg.Connection, reviewer_id: int, decisions: list[Decision]) -> Counter:
    """全部在同一個交易：任何一筆失敗（內容已變、權限不足、不允許的轉換）就全部不生效。"""
    result: Counter = Counter()
    with conn.transaction():
        for d in decisions:
            v = _version(conn, d.doc_key, d.version_label)
            row = conn.execute(
                "SELECT id, content_sha256 FROM chunks WHERE document_version_id = %s AND chunk_key = %s",
                (v["id"], d.chunk_key),
            ).fetchone()
            if row is None:
                raise IngestError(f"找不到片段：{d.doc_key} {v['version_label']} {d.chunk_key}")
            expected = d.content_sha256 or row["content_sha256"]
            outcome = conn.execute(
                "SELECT review_chunk(%s, %s, %s, %s, %s) AS r",
                (row["id"], reviewer_id, d.decision, d.reason, expected),
            ).fetchone()["r"]
            result[outcome] += 1
    return result


def publish(conn: psycopg.Connection, doc_key: str, label: str, allow_empty: bool = False,
            actor: str = "ingest.publish", accept_candidate_impact: bool = False) -> dict:
    """發布 draft 版本（取代目前 published）。S9 起改走 release.switch：同一交易內重新驗證、切換版本與索引。

    沒有任何已核准片段時預設拒絕；目前有 active 索引時，須先用 `python -m app.release prepare` 準備新索引，
    再用 `python -m app.release switch --index ...` 切換（這裡不會呼叫嵌入模型）。
    """
    from . import release  # 延後匯入：release 依賴 retrieval，避免匯入循環
    try:
        out = release.switch(conn, doc_key, label, actor=actor, allow_empty=allow_empty,
                             accept_candidate_impact=accept_candidate_impact)
    except release.ReleaseError as exc:
        raise IngestError(str(exc)) from None
    return {k: out[k] for k in ("doc_key", "published", "retired", "approved", "pending", "rejected",
                                "vectors", "candidate_impact")}
# 核心邏輯：保留 S3 的介面與回傳欄位（測試與審核頁沿用），實際切換規則集中在 release.py 一處。


# ---------------------------------------------------------------------------
# 指令列
# ---------------------------------------------------------------------------
def _connect() -> psycopg.Connection:
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("請設定 DATABASE_URL")
    return psycopg.connect(url, autocommit=True, row_factory=dict_row)


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m app.ingest")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("inventory", help="盤點 ETL 輸出（不連資料庫）")
    p.add_argument("--root", type=Path, required=True)
    p = sub.add_parser("import", help="匯入成 draft 版本")
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--doc", nargs="*", help="只匯入指定 doc_id；省略 = 登錄清單全部")
    p.add_argument("--units", type=Path, help="A 的支持單位 JSONL（同目錄需有 manifest.json）")
    sub.add_parser("status", help="各文件版本與審核數量")
    p = sub.add_parser("review-list", help="匯出審核清單 CSV（含原文，不進 git）")
    p.add_argument("--doc", required=True)
    p.add_argument("--version")
    p.add_argument("--status", nargs="+", default=["pending"], choices=["pending", "approved", "rejected"])
    p.add_argument("--out", type=Path)
    p = sub.add_parser("review", help="人工審核（逐筆決定＋理由）")
    p.add_argument("--reviewer", required=True)
    p.add_argument("--password-stdin", action="store_true")
    p.add_argument("--decisions", type=Path, help="review-list 匯出後填好 decision／reason 的 CSV")
    p.add_argument("--doc")
    p.add_argument("--version")
    p.add_argument("--chunk-key")
    p.add_argument("--decision", choices=["approve", "reject"])
    p.add_argument("--reason")
    p = sub.add_parser("verify-units", help="唯讀核對資料庫片段與 A 的支持單位逐字一致（修復 D）")
    p.add_argument("--units", type=Path, required=True)
    p.add_argument("--version", help="指定版本標籤；省略＝每份文件最新版本")
    p = sub.add_parser("publish", help="發布 draft 版本（取代目前 published）")
    p.add_argument("--doc", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--allow-empty", action="store_true")
    p = sub.add_parser("set-visibility", help="設定文件可見範圍")
    p.add_argument("--doc", required=True)
    p.add_argument("--visibility", required=True, choices=["all", "admin"])
    args = ap.parse_args(argv)

    try:
        if args.cmd == "inventory":
            _print(inventory(args.root.resolve()))
            return
        if args.cmd == "import":
            root = args.root.resolve()
            docs, sources = load_registry(root)
            ids = args.doc or [d["doc_id"] for d in docs]
            unknown = set(ids) - {d["doc_id"] for d in docs}
            if unknown:
                raise IngestError(f"不在 config/documents.json 的文件不能匯入：{sorted(unknown)}")
            units, unit_summary = load_support_units(args.units.resolve()) if args.units else ({}, None)
            if unit_summary:
                print(json.dumps({"support_units": unit_summary}, ensure_ascii=False), file=sys.stderr)
            loaded = [load_document(root, i, sources, units.get(i)) for i in ids]  # 全部讀取成功才開始寫入
            with _connect() as conn:
                _print([{**import_document(conn, d), "raw_verified": d.raw_verified,
                         "flags": dict(d.flag_counts)} for d in loaded])
            return
        with _connect() as conn:
            if args.cmd == "status":
                _print(status(conn))
            elif args.cmd == "review-list":
                rows = review_rows(conn, args.doc, args.version, args.status)
                label = rows[0]["version_label"] if rows else (args.version or "latest")
                out = args.out or Path("data/review/s3") / f"{args.doc}.{label}.review.csv"
                write_review_csv(rows, out)
                print(f"{len(rows)} 筆 → {out}（含原文，請勿提交 git）")
            elif args.cmd == "review":
                if args.decisions:
                    decisions = read_decisions(args.decisions)
                elif args.doc and args.chunk_key and args.decision and args.reason:
                    decisions = [Decision(args.doc, args.version, args.chunk_key, args.decision, args.reason, None)]
                else:
                    raise IngestError("請給 --decisions，或 --doc --chunk-key --decision --reason")
                if not decisions:
                    raise IngestError("審核檔沒有任何已填 decision 的列")
                password = (sys.stdin.readline().rstrip("\r\n") if args.password_stdin
                            else getpass.getpass(f"{args.reviewer} 的密碼："))
                reviewer_id = authenticate_admin(conn, args.reviewer, password)
                _print(dict(apply_decisions(conn, reviewer_id, decisions)))
            elif args.cmd == "verify-units":
                by_doc, summary = load_support_units(args.units.resolve())
                _print({"support_units": summary, **verify_units(conn, by_doc, args.version)})
            elif args.cmd == "publish":
                _print(publish(conn, args.doc, args.version, args.allow_empty, actor=f"cli:{getpass.getuser()}"))
            elif args.cmd == "set-visibility":
                from . import release  # S9：可見範圍變更一律寫入發布事件
                try:
                    release.set_visibility(conn, args.doc, args.visibility, actor=f"cli:{getpass.getuser()}")
                except release.ReleaseError as exc:
                    raise IngestError(str(exc)) from None
                print(f"{args.doc} visibility = {args.visibility}")
    except (IngestError, psycopg.Error) as exc:
        sys.exit(f"失敗：{exc}")


if __name__ == "__main__":
    main()
