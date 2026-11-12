"""SYNTHETIC 展示種子（app.demo_seed）：冪等、只建 SYN_ 文件、需管理者密碼、核准逐筆留紀錄。

SYNTHETIC：本檔用的是合成候選檔與合成文件，只驗證種子指令照設計運作，不是正式驗收。
"""
from __future__ import annotations

import copy
import io
import json

import psycopg
import pytest
from psycopg.rows import dict_row

from app import demo_seed
from app import preferences as pref
from app import recommend as rec
from app.rec_cases import SYNTHETIC_FILE

from .conftest import PASSWORD

SPEC = json.loads(SYNTHETIC_FILE.read_text(encoding="utf-8"))


@pytest.fixture
def conn(db, users):
    with psycopg.connect(db, autocommit=True, row_factory=dict_row) as c:
        yield c


# 回傳資料庫目前的各項數量，用來比對重跑前後沒有多出資料。
def _counts(conn) -> dict:
    q = {
        "documents": "SELECT count(*) AS n FROM documents",
        "versions": "SELECT count(*) AS n FROM document_versions",
        "chunks": "SELECT count(*) AS n FROM chunks",
        "approved": "SELECT count(*) AS n FROM chunks WHERE review_status = 'approved'",
        "review_events": "SELECT count(*) AS n FROM chunk_review_events",
        "options": "SELECT count(*) AS n FROM recommendation_options",
        "active_options": "SELECT count(*) AS n FROM recommendation_options WHERE status = 'active'",
    }
    return {k: conn.execute(sql).fetchone()["n"] for k, sql in q.items()}


# 以命令列入口執行種子（密碼從 stdin），回傳 SystemExit 或 None。
def _run_cli(monkeypatch, db, admin: str, password: str, *extra: str):
    monkeypatch.setenv("DATABASE_URL", db)
    monkeypatch.setattr("sys.stdin", io.StringIO(password + "\n"))
    try:
        demo_seed.main(["--admin", admin, "--password-stdin", *extra])
    except SystemExit as exc:
        return exc
    return None


def test_seed_is_idempotent_and_marks_everything_synthetic(conn, users):
    first = demo_seed.seed(conn, SPEC, users["root"])
    n_chunks = len(demo_seed.plan_documents(SPEC)["SYN_S7_A"]) + len(demo_seed.plan_documents(SPEC)["SYN_S7_B"])
    after_first = _counts(conn)
    assert after_first == {"documents": 2, "versions": 2, "chunks": n_chunks, "approved": n_chunks,
                           "review_events": n_chunks, "options": 5, "active_options": 5}
    assert {c["result"] for c in first["candidates"]} == {"activated"}

    second = demo_seed.seed(conn, SPEC, users["root"])
    assert _counts(conn) == after_first                          # 重跑不多建任何東西、不多寫審核事件
    assert all(d["document"] == "existing" and d["chunks_created"] == 0 and d["chunks_approved"] == 0
               and not d["published_now"] for d in second["documents"])
    assert {c["result"] for c in second["candidates"]} == {"unchanged (active)"}

    rows = conn.execute(
        """SELECT d.doc_key, d.bibliography, dv.status, c.chunk_key, c.quote_text, c.page_start, c.review_note,
                  u.username
             FROM chunks c JOIN document_versions dv ON dv.id = c.document_version_id
             JOIN documents d ON d.id = dv.document_id JOIN users u ON u.id = c.reviewed_by""").fetchall()
    for r in rows:
        assert r["doc_key"].startswith("SYN_") and r["bibliography"]["synthetic"] is True
        assert r["status"] == "published" and r["page_start"] == 1 and r["username"] == "root"
        # e2e/tests/flow.spec.ts 依賴這個字樣
        assert r["quote_text"] == f"SYNTHETIC 合成證據（展示用，非正式來源）：{r['chunk_key']}"
        assert "SYNTHETIC" in r["review_note"] and "非正式核准" in r["review_note"]

    # 種子完成後推薦可用，且標示為合成
    state = pref.empty_state()
    parsed = pref.RuleParser().parse("上班用的自然唇色，不要霧面")
    state = pref.process_parsed(state, parsed, request_id=None, now="2026-10-16T00:00:00+00:00").state
    d = rec.run(conn, state, role="user", intent=parsed.intent, secondary=parsed.secondary_intents)
    assert d.status == "recommended" and d.synthetic
    assert [r["option_key"] for r in d.recommendations][:2] == ["synthetic-nude-satin", "synthetic-rose-satin"]


def test_rejects_non_syn_document_without_writing(conn, users):
    bad = copy.deepcopy(SPEC)
    for c in bad["candidates"]:
        for e in c["evidence"]:
            if e["doc_key"] == "SYN_S7_B":
                e["doc_key"] = "TH013"
    with pytest.raises(demo_seed.SeedError, match="只接受 SYN_"):
        demo_seed.seed(conn, bad, users["root"])
    assert _counts(conn)["documents"] == 0 and _counts(conn)["chunks"] == 0


def test_rejects_non_synthetic_spec_and_foreign_existing_document(conn, users):
    real = copy.deepcopy(SPEC)
    real["synthetic"] = False
    with pytest.raises(demo_seed.SeedError, match="synthetic=true"):
        demo_seed.seed(conn, real, users["root"])
    # 同名文件已存在但不是本指令建立的合成文件 → 不覆寫、整批不生效
    conn.execute("INSERT INTO documents (doc_key, title, doc_type, visibility, rag_eligible) "
                 "VALUES ('SYN_S7_B', '別人的文件', 'other', 'admin', TRUE)")
    with pytest.raises(demo_seed.SeedError, match="不是合成展示文件"):
        demo_seed.seed(conn, SPEC, users["root"])
    assert _counts(conn)["documents"] == 1 and _counts(conn)["chunks"] == 0


def test_cli_requires_admin_password(monkeypatch, db, users):
    exc = _run_cli(monkeypatch, db, "root", "wrong-password-123")
    assert exc is not None and "密碼錯誤" in str(exc.code)
    exc = _run_cli(monkeypatch, db, "alice", PASSWORD)            # 一般使用者不可執行
    assert exc is not None and "管理者" in str(exc.code)
    with psycopg.connect(db, row_factory=dict_row) as c:
        assert _counts(c)["documents"] == 0


def test_cli_seeds_with_correct_password_and_rejects_bad_file(monkeypatch, db, users, tmp_path, capsys):
    assert _run_cli(monkeypatch, db, "root", PASSWORD) is None
    out = json.loads(capsys.readouterr().out)
    assert out["synthetic"] is True and len(out["candidates"]) == 5
    bad = copy.deepcopy(SPEC)
    bad["candidates"][0]["evidence"][0]["doc_key"] = "REAL_DOC"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    exc = _run_cli(monkeypatch, db, "root", PASSWORD, "--file", str(path))
    assert exc is not None and "只接受 SYN_" in str(exc.code)
