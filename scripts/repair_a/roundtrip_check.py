"""修復 A：支持單位 round-trip 核對——匯出 → backend 匯入 → 查回 → 實際模型輸入，逐欄比對 SHA。

用法（用 backend 的 venv，需要 psycopg）：
    python scripts/repair_a/roundtrip_check.py --root <資料根目錄> --out <報告.json> [--units ID ...] [--db-url URL --version v1]

- 匯入：呼叫本工作樹 backend/app/ingest.py 的 load_document（不寫 DB），看 ChunkIn 帶了哪些欄位。
- 查回：給 --db-url 時對 chunks 表做唯讀 SELECT（D 在隔離庫匯入後用）；沒給時依 retrieval.context_chunks
  的欄位清單由 ChunkIn 組出同形狀的列，標 offline_simulated，不冒稱連過 DB。
- 模型輸入：把查回列交給 qa.build_messages，解析 user 訊息裡的 evidence JSON，比對實際送進模型的內容。
- 沒指定 --units 時，自動挑真實資料中各一個：有第二定位、有原文條件、有商品指涉、網頁、PDF。
欄位狀態：kept（一路相符）、lost@import／lost@query／lost@model_input（在那一站消失或不符）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "backend"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from app import ingest, qa, retrieval  # noqa: E402

# retrieval.context_chunks 從 usable_chunks 取的欄位（@38963d0）；查回離線模擬只給這些欄位
CONTEXT_COLUMNS = ["chunk_id", "chunk_key", "doc_key", "version_label", "title", "page_start", "page_end",
                   "html_locator", "section", "quote_text", "citation"]
# 契約欄位 → 在 ChunkIn.attributes 裡應叫的鍵（D 接入後應出現；現在不在 = lost@import）
ATTR_FIELDS = {
    "locator_secondary": "locator_secondary",
    "conditions": "conditions",
    "links": "links",
    "product_mentions": "product_mentions",
    "content_role": "content_role",
    "support_scope": "support_scope",
    "eligibility": "eligibility",
}


def sha(s: str | None) -> str | None:
    return None if s is None else hashlib.sha256(s.encode("utf-8")).hexdigest()


def pick_units(units: list[dict]) -> list[str]:
    tests = [
        ("有第二定位", lambda u: u["locator_secondary"]),
        ("有原文條件", lambda u: u["conditions"] and u["locator_primary"]["type"] == "html_dom"),
        ("有商品指涉", lambda u: u["product_mentions"]),
        ("PDF 有原文條件", lambda u: u["conditions"] and u["locator_primary"]["type"] == "pdf_page"),
    ]
    out = []
    for _, t in tests:
        hit = next((u["unit_id"] for u in units if t(u) and u["unit_id"] not in out), None)
        if hit:
            out.append(hit)
    return out


def query_back_db(db_url: str, doc_key: str, version: str, keys: list[str]) -> dict[str, dict]:
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        conn.read_only = True
        rows = conn.execute(
            """SELECT c.id AS chunk_id, c.chunk_key, d.doc_key, dv.version_label, d.title, c.page_start, c.page_end,
                      c.html_locator, c.section, c.quote_text, c.search_text, c.attributes,
                      c.attributes -> 'citation' AS citation
                 FROM chunks c JOIN document_versions dv ON dv.id = c.document_version_id
                 JOIN documents d ON d.id = dv.document_id
                WHERE d.doc_key = %s AND dv.version_label = %s AND c.chunk_key = ANY(%s)""",
            (doc_key, version, keys)).fetchall()
    return {r["chunk_key"]: r for r in rows}


def check_unit(u: dict, chunk: ingest.ChunkIn | None, doc: ingest.DocIn, row: dict | None,
               model_item: dict | None) -> dict:
    fields: dict[str, dict] = {}

    def status(stages: list[tuple[str, bool]]) -> str:
        for name, ok in stages:
            if not ok:
                return f"lost@{name}"
        return "kept"

    imp_text = chunk.quote_text if chunk else None
    q_text = row.get("quote_text") if row else None
    m_text = model_item.get("text") if model_item else None
    fields["text"] = {"export": u["text_sha256"], "import": sha(imp_text), "query": sha(q_text), "model_input": sha(m_text),
                      "status": status([("import", sha(imp_text) == u["text_sha256"]),
                                        ("query", sha(q_text) == u["text_sha256"]),
                                        ("model_input", sha(m_text) == u["text_sha256"])])}
    imp_search = chunk.search_text if chunk else None
    q_search = row.get("search_text") if row and "search_text" in row else None
    fields["search_text"] = {
        "export": u["search_text_sha256"], "export_base": u["search_text_base_sha256"], "import": sha(imp_search),
        "query": sha(q_search) if row and "search_text" in row else "not_in_context_columns",
        "status": status([("import", sha(imp_search) == u["search_text_sha256"])]),
        "note": "search_text 只用於 embedding，不進模型輸入；import 只等於 export_base 代表新搜尋文字沒被匯入"}
    lp = u["locator_primary"]
    if lp["type"] == "pdf_page":
        exp_loc = lp["text"]
        imp_loc = qa.locator_text({"page_start": chunk.page_start, "page_end": chunk.page_end}) if chunk else None
        q_loc = qa.locator_text(row) if row else None
    else:
        exp_loc = (lp["dom_paths"] or [None])[0]
        imp_loc = chunk.html_locator if chunk else None
        q_loc = row.get("html_locator") if row else None
    m_loc = model_item.get("locator") if model_item else None
    fields["locator_primary"] = {"export": exp_loc, "import": imp_loc, "query": q_loc, "model_input": m_loc,
                                 "status": status([("import", imp_loc == exp_loc), ("query", q_loc == exp_loc),
                                                   ("model_input", m_loc == exp_loc)])}
    fields["section_original"] = {
        "export": u["section_original"], "import": chunk.section if chunk else None,
        "query": row.get("section") if row else None, "model_input": model_item.get("section") if model_item else None,
        "status": status([("import", chunk is not None and chunk.section == u["section_original"]),
                          ("query", row is not None and row.get("section") == u["section_original"]),
                          ("model_input", model_item is not None and model_item.get("section") == u["section_original"])])}
    fields["source_version"] = {"export": u["source_version"], "import": doc.source_sha256,
                                "model_input": model_item.get("version") if model_item else None,
                                "status": status([("import", doc.source_sha256 == u["source_version"])]),
                                "note": "模型輸入只帶 DB 版本標籤（v1…），不帶來源 SHA；版本對應在 document_versions"}
    attrs = chunk.attributes if chunk else {}
    q_attrs = (row.get("attributes") or {}) if row and "attributes" in row else None
    for f, key in ATTR_FIELDS.items():
        exp = u[f]
        imp = attrs.get(key, "<missing>")
        q = q_attrs.get(key, "<missing>") if q_attrs is not None else "<not_in_context_columns>"
        m = model_item.get(f, "<missing>") if model_item else None
        empty = exp in (None, [], {})
        fields[f] = {"export": exp, "import": imp, "query": q, "model_input": m,
                     "status": ("empty_in_export" if empty else
                                status([("import", imp == exp), ("query", q == exp), ("model_input", m == exp)]))}
    return {"unit_id": u["unit_id"], "source_id": u["source_id"], "fields": fields}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="支持單位 round-trip 逐欄核對")
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--units-file", type=Path)
    ap.add_argument("--units", nargs="*")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--db-url")
    ap.add_argument("--version", help="--db-url 時要查的文件版本標籤")
    ap.add_argument("--question", default="（round-trip 核對用固定字串，不是評測題）")
    a = ap.parse_args(argv)
    root = a.root.resolve()
    units_path = a.units_file or root / "data" / "processed" / "support_units" / "support_units.jsonl"
    units = [json.loads(line) for line in units_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    by_id = {u["unit_id"]: u for u in units}
    chosen = a.units or pick_units(units)
    _, sources = ingest.load_registry(root)
    docs: dict[str, ingest.DocIn] = {}
    results = []
    for uid in chosen:
        u = by_id[uid]
        doc = docs.get(u["source_id"]) or docs.setdefault(u["source_id"], ingest.load_document(root, u["source_id"], sources))
        chunk = next((c for c in doc.chunks if c.chunk_key == uid), None)
        if a.db_url:
            row = query_back_db(a.db_url, u["source_id"], a.version, [uid]).get(uid)
            mode = "db_readonly"
        else:
            row = None if chunk is None else {
                "chunk_id": chunk.ordinal, "chunk_key": chunk.chunk_key, "doc_key": doc.doc_key,
                "version_label": "(offline)", "title": doc.title, "page_start": chunk.page_start,
                "page_end": chunk.page_end, "html_locator": chunk.html_locator, "section": chunk.section,
                "quote_text": chunk.quote_text, "citation": chunk.attributes.get("citation")}
            mode = "offline_simulated"
        model_item = None
        if row is not None:
            hit = retrieval.Hit(rank=1, chunk_id=row["chunk_id"], chunk_key=uid, doc_key=row["doc_key"],
                                version_label=row["version_label"], page_start=row["page_start"],
                                page_end=row["page_end"], content_sha256="", index_label="(roundtrip)", distance=0.0)
            msgs = qa.build_messages(a.question, [qa.Evidence(label="E1", hit=hit, chunk=row)])
            payload = re.search(r"<data>\n(.*)\n</data>", msgs[1]["content"], re.S).group(1)
            model_item = json.loads(payload)["evidence"][0]
        r = check_unit(u, chunk, doc, row, model_item)
        r["query_mode"] = mode
        r["model_input_keys"] = sorted(model_item) if model_item else None
        results.append(r)
    summary: dict[str, dict] = {}
    for r in results:
        for f, v in r["fields"].items():
            summary.setdefault(f, {})
            summary[f][v["status"]] = summary[f].get(v["status"], 0) + 1
    report = {"root_sha_manifest": hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest()
              if (root / "manifest.json").is_file() else None,
              "units_file_sha256": hashlib.sha256(units_path.read_bytes()).hexdigest(),
              "backend_code": "本工作樹 backend/app（ingest／retrieval／qa 未修改）",
              "context_columns": CONTEXT_COLUMNS, "units": chosen, "summary": summary, "results": results}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for f, s in summary.items():
        print(f"{f:20s} {s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
