"""網頁（HTML）來源匯入與 S12 審核流程。

文件內容一律是 SYNTHETIC 測試資料（暫存目錄產生，格式仿 ETL 對網頁的輸出），非真實文獻。
重點：網頁不虛構頁碼、定位可追溯（網址＋DOM 路徑＋標題階層）、重匯不新增重複資料、不覆寫已核准審核、
PDF 原有指紋不變。
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from app import ingest
from app.ingest import IngestError

from .conftest import PASSWORD, login
from .test_ingest import DOC as PDF_DOC
from .test_ingest import _records as _pdf_records
from .test_ingest import _write_root as _write_pdf_root

WEB = "SYN_WEB"
RAW = b"<html><body>SYNTHETIC web page</body></html>"
URL = "https://example.test/synthetic-lip-article"
BASE = "body[1]>main[1]>article[1]>"


def _blocks() -> list[dict]:
    """cleaned 區塊（依文件順序）：h1 → h2 → 兩段 → 另一個 h2 → 一段；一個被移除的 h2 不算標題。"""
    def blk(n, block_type, text, loc, level=None, removed=False):
        return {"block_id": f"{WEB}_h{n:04d}", "block_type": block_type, "heading_level": level,
                "cleaned_text": text, "source_locator": BASE + loc, "is_removed": removed}
    return [
        blk(1, "heading", "SYNTHETIC lip guide", "h1[1]", 1),
        blk(2, "heading", "Choosing a shade", "h2[1]", 2),
        blk(3, "paragraph", "SYNTHETIC: pick a shade close to your natural lip.", "p[1]"),
        blk(4, "paragraph", "SYNTHETIC: continued sentence in the next paragraph.", "p[2]"),
        blk(5, "heading", "Shop now", "h2[2]", 2, removed=True),
        blk(6, "heading", "Finish", "h2[3]", 2),
        blk(7, "paragraph", "SYNTHETIC: matte finishes look less glossy.", "p[3]"),
    ]


def _records() -> list[dict]:
    base = {"doc_id": WEB, "block_type": "paragraph", "pages": [], "cited_works": [],
            "attribution": "no_citation_marker", "data_status": None, "data_note": None,
            "linked_block_ids": [], "citation": "(Syn, 2026)", "reference": "Syn (2026). [web article]",
            "continues_from_block_id": None}
    return [
        {**base, "block_id": f"{WEB}_h0003", "block_ids": [f"{WEB}_h0003", f"{WEB}_h0004"],
         "section": "Choosing a shade", "text": "pick a shade",
         "display_text": "SYNTHETIC: pick a shade close to your natural lip. Continued sentence."},
        {**base, "block_id": f"{WEB}_h0007", "block_ids": [f"{WEB}_h0007"], "section": "Finish",
         "text": "matte", "display_text": "SYNTHETIC: matte finishes look less glossy."},
    ]


def _write_root(root: Path, records: list[dict], blocks: list[dict] | None = None, *, url: str | None = URL,
                bib_url: str | None = None) -> Path:
    for d in ("config", "data/raw/web", "data/processed/cleaned", "data/processed/rag_ready"):
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / "data/raw/web/syn.html").write_bytes(RAW)
    (root / "config/documents.json").write_text(json.dumps(
        {"documents": [{"doc_id": WEB, "path": "data/raw/web/syn.html"}]}), encoding="utf-8")
    (root / "config/sources.json").write_text(json.dumps({WEB: {
        "title": "SYNTHETIC web article", "authors": [], "author_citation": "Syn", "year": 2026,
        "doc_type": "web article（SYNTHETIC）", "url": bib_url, "verified_from": "SYNTHETIC"}}), encoding="utf-8")
    manifest = {"doc_id": WEB, "source_path": "data/raw/web/syn.html", "source_type": "html",
                "sha256": hashlib.sha256(RAW).hexdigest(), "file_size_bytes": len(RAW), "source_url": url,
                "total_pages": None, "extractor": "beautifulsoup4 test", "extracted_at": "2026-10-17T00:00:00+00:00"}
    cleaned = {"schema_version": "1", "stage": "cleaned", "manifest": manifest,
               "blocks": _blocks() if blocks is None else blocks, "issues": [], "pages": []}
    (root / f"data/processed/cleaned/{WEB}.json").write_text(json.dumps(cleaned), encoding="utf-8")
    (root / f"data/processed/rag_ready/{WEB}.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
    return root


def _load(root: Path, doc_id: str = WEB):
    _, sources = ingest.load_registry(root)
    return ingest.load_document(root, doc_id, sources)


@pytest.fixture
def root(tmp_path):
    return _write_root(tmp_path / "repo", _records())


@pytest.fixture
def conn(db, users):
    with psycopg.connect(db, autocommit=True, row_factory=dict_row) as c:
        yield c


def _chunks(conn, label="v1"):
    return conn.execute(
        """SELECT c.* FROM chunks c JOIN document_versions dv ON dv.id = c.document_version_id
             JOIN documents d ON d.id = dv.document_id
            WHERE d.doc_key = %s AND dv.version_label = %s ORDER BY c.ordinal""", (WEB, label)).fetchall()


# ---------------------------------------------------------------------------
# 定位：不虛構頁碼，網址＋DOM 路徑＋標題階層可追溯
# ---------------------------------------------------------------------------
def test_html_import_has_dom_locator_and_no_page(conn, root):
    assert ingest.import_document(conn, _load(root))["result"] == "created"
    rows = _chunks(conn)
    assert [r["page_start"] for r in rows] == [None, None] and [r["page_end"] for r in rows] == [None, None]
    assert [r["html_locator"] for r in rows] == [BASE + "p[1]", BASE + "p[3]"]
    merged, single = rows[0]["attributes"], rows[1]["attributes"]
    assert merged["html_locators"] == [BASE + "p[1]", BASE + "p[2]"]  # 併段時每段定位都保留
    assert merged["heading_path"] == ["SYNTHETIC lip guide", "Choosing a shade"]
    assert single["heading_path"] == ["SYNTHETIC lip guide", "Finish"]  # 被移除的「Shop now」不算
    assert merged["source_url"] == URL
    assert conn.execute("SELECT doc_type FROM documents WHERE doc_key = %s", (WEB,)).fetchone()["doc_type"] == "web_article"
    assert {r["review_status"] for r in rows} == {"pending"}


def test_inventory_reports_html_locator_instead_of_pages(root):
    row = next(r for r in ingest.inventory(root)["documents"] if r["doc_id"] == WEB)
    assert "error" not in row and row["chunks"] == 2 and "網頁" in row["pages"]


def test_html_falls_back_to_bibliography_url(tmp_path):
    doc = _load(_write_root(tmp_path / "r", _records(), url=None, bib_url=URL))
    assert doc.chunks[0].attributes["source_url"] == URL


@pytest.mark.parametrize("case,message", [
    ("page", "帶頁碼"), ("no_locator", "找不到網頁段落定位"), ("missing_block", "找不到網頁段落定位"), ("no_url", "網址"),
])
def test_html_rejects_untraceable_chunks(tmp_path, case, message):
    recs, blocks, url = _records(), _blocks(), URL
    if case == "page":
        recs[0]["pages"] = [1]  # 網頁不得有頁碼
    elif case == "no_locator":
        blocks[2]["source_locator"] = None
    elif case == "missing_block":
        blocks = [b for b in blocks if b["block_id"] != f"{WEB}_h0004"]
    else:
        url = None
    with pytest.raises(IngestError, match=message):
        _load(_write_root(tmp_path / case, recs, blocks, url=url))


# ---------------------------------------------------------------------------
# 重匯：不新增重複、不覆寫已核准
# ---------------------------------------------------------------------------
def test_reimport_unchanged_and_new_version_keeps_approval(conn, root, tmp_path):
    ingest.import_document(conn, _load(root))
    rid = ingest.authenticate_admin(conn, "root", PASSWORD)
    ingest.apply_decisions(conn, rid, [ingest.Decision(WEB, "v1", f"{WEB}_h0003", "approve", "已對照合成網頁", None)])
    n_chunks = conn.execute("SELECT count(*) AS n FROM chunks").fetchone()["n"]

    again = ingest.import_document(conn, _load(root))
    assert (again["result"], again["version_label"]) == ("unchanged", "v1")
    assert conn.execute("SELECT count(*) AS n FROM chunks").fetchone()["n"] == n_chunks

    # 網頁改版（另一段內容變了）→ 新版本；舊版本的核准原封不動
    recs = copy.deepcopy(_records())
    recs[1]["display_text"] = "SYNTHETIC: matte finishes changed in a site update."
    assert ingest.import_document(conn, _load(_write_root(tmp_path / "v2", recs)))["version_label"] == "v2"
    v1 = {r["chunk_key"]: r for r in _chunks(conn, "v1")}
    assert v1[f"{WEB}_h0003"]["review_status"] == "approved"
    assert conn.execute("SELECT count(*) AS n FROM chunk_review_events").fetchone()["n"] == 1
    v2 = {r["chunk_key"]: r for r in ingest.review_rows(conn, WEB, "v2", ["pending"])}
    assert v2[f"{WEB}_h0003"]["prior_decision"] == "approved (v1)"  # 只提示
    assert v2[f"{WEB}_h0003"]["review_status"] == "pending"


def test_locator_change_is_a_new_version(conn, root, tmp_path):
    ingest.import_document(conn, _load(root))
    blocks = _blocks()
    blocks[6]["source_locator"] = BASE + "section[1]>p[1]"  # 只有定位改變也算內容不同
    assert ingest.import_document(conn, _load(_write_root(tmp_path / "loc", _records(), blocks)))["result"] == "created"


# ---------------------------------------------------------------------------
# PDF 不退化：指紋內容與修改前相同（既有 PDF 版本重匯仍為 unchanged）
# ---------------------------------------------------------------------------
def test_pdf_fingerprint_unchanged(tmp_path):
    root = _write_pdf_root(tmp_path / "pdf", _pdf_records())
    doc = _load(root, PDF_DOC)
    for c, rec in zip(doc.chunks, _pdf_records()):
        assert c.html_locator is None and c.page_start is not None
        assert set(c.attributes) >= {"block_ids", "review_flags"} and "html_locators" not in c.attributes
        legacy = {"chunk_key": c.chunk_key, "section": c.section, "pages": sorted(rec["pages"]),
                  "quote_text": c.quote_text, "search_text": c.search_text, "attributes": c.attributes}
        assert c.content_sha256 == hashlib.sha256(
            json.dumps(legacy, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_pdf_without_pages_still_rejected(tmp_path):
    recs = _pdf_records()
    recs[0]["pages"] = []
    with pytest.raises(IngestError, match="PDF 來源必須有頁碼"):
        _load(_write_pdf_root(tmp_path / "pdf", recs), PDF_DOC)


# ---------------------------------------------------------------------------
# 端到端：網頁片段走完 S12 審核 → 發布 → 一般使用者看到網頁定位
# ---------------------------------------------------------------------------
def test_html_end_to_end_review_publish_and_sources(client, db, users, root):
    with psycopg.connect(db, autocommit=True, row_factory=dict_row) as c:
        ingest.import_document(c, _load(root))
    h = login(client, "root")
    docs = client.get("/api/admin/review/documents", headers=h).json()["items"]
    assert any(d["doc_key"] == WEB and d["version_label"] == "v1" for d in docs)

    page = client.get("/api/admin/review/chunks", params={"doc": WEB}, headers=h).json()
    assert page["total"] == 2
    item = page["items"][0]
    assert item["pages"] is None and item["html_locator"] == BASE + "p[1]" and item["section"] == "Choosing a shade"

    r = client.post(f"/api/admin/review/chunks/{item['id']}", headers=h, json={
        "decision": "approve", "reason": "已對照合成網頁 p[1]", "content_sha256": item["content_sha256"],
        "doc_key": WEB, "version_label": "v1", "chunk_key": item["chunk_key"]})
    assert r.status_code == 200 and r.json()["result"] == "approved"
    events = client.get(f"/api/admin/review/chunks/{item['id']}/events", headers=h).json()["items"]
    assert [(e["reviewer"], e["to_status"]) for e in events] == [("root", "approved")]

    assert client.post("/api/admin/review/publish", headers=h,
                       json={"doc_key": WEB, "version_label": "v1"}).status_code == 200
    assert client.put(f"/api/admin/documents/{WEB}/visibility", headers=h,
                      json={"visibility": "all"}).status_code == 200
    src = client.get("/api/sources/chunks", headers=login(client, "alice")).json()
    assert src["total"] == 1
    got = src["items"][0]
    assert (got["chunk_key"], got["page_start"], got["html_locator"]) == (f"{WEB}_h0003", None, BASE + "p[1]")

    # 發布後重匯同內容：不新增版本、核准不變
    with psycopg.connect(db, autocommit=True, row_factory=dict_row) as c:
        assert ingest.import_document(c, _load(root))["result"] == "unchanged"
        assert c.execute("SELECT review_status FROM chunks WHERE chunk_key = %s",
                         (f"{WEB}_h0003",)).fetchone()["review_status"] == "approved"


def test_review_csv_has_html_locator_column(conn, root, tmp_path):
    ingest.import_document(conn, _load(root))
    out = tmp_path / "review.csv"
    ingest.write_review_csv(ingest.review_rows(conn, WEB, None, ["pending"]), out)
    header = out.read_text(encoding="utf-8-sig").splitlines()[0].split(",")
    assert header[:5] == ["doc_key", "version_label", "chunk_key", "pages", "html_locator"]
    assert BASE + "p[1]" in out.read_text(encoding="utf-8-sig")
