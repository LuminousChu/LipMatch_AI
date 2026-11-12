"""run_all.py 的測試：用合成 PDF 與暫存資料夾，不碰 data/ 底下的真實輸出。"""

from __future__ import annotations

import json

import pytest

from tests.conftest import ROOT, _load


@pytest.fixture(scope="module")
def run_all_mod():
    return _load("lipmatch_run_all", "run_all.py")


def _write_config(tmp_path, docs):
    cfg = tmp_path / "documents.json"
    cfg.write_text(json.dumps({"documents": docs}, ensure_ascii=False), encoding="utf-8")
    return cfg


def _write_sources(tmp_path, doc_ids):
    src = tmp_path / "sources.json"
    src.write_text(json.dumps({d: {
        "title": "Synthetic test document", "authors": ["Tester"], "author_citation": "Tester, T.",
        "year": 2026, "verified_from": "synthetic fixture"} for d in doc_ids}), encoding="utf-8")
    return src


def _rel(path):
    """documents.json 的路徑相對於專案根目錄；暫存檔在別的磁碟時改用絕對路徑。"""
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path.resolve())


def test_runs_all_steps_for_registered_documents(run_all_mod, tmp_path, synthetic_pdf):
    cfg = _write_config(tmp_path, [{"doc_id": "SYN_A", "path": _rel(synthetic_pdf)}])
    src = _write_sources(tmp_path, ["SYN_A"])
    out = tmp_path / "data"
    code = run_all_mod.main(["--config", str(cfg), "--sources", str(src), "--data-dir", str(out)])
    assert code == 0
    for p in ["interim/extracted/SYN_A.json", "processed/cleaned/SYN_A.json",
              "review/SYN_A.integrity_report.md", "processed/rag_ready/SYN_A.jsonl"]:
        assert (out / p).exists(), p


def test_missing_file_is_skipped_not_fatal(run_all_mod, tmp_path, synthetic_pdf, capsys):
    cfg = _write_config(tmp_path, [{"doc_id": "GONE", "path": "data/raw/not_here.pdf"},
                                   {"doc_id": "SYN_B", "path": _rel(synthetic_pdf)}])
    src = _write_sources(tmp_path, ["SYN_B"])
    code = run_all_mod.main(["--config", str(cfg), "--sources", str(src), "--data-dir", str(tmp_path / "data")])
    out = capsys.readouterr().out
    assert code == 0
    assert "略過  GONE" in out and "完成  SYN_B" in out
    assert (tmp_path / "data" / "processed" / "cleaned" / "SYN_B.json").exists()


def test_missing_source_skips_rag_export_only(run_all_mod, tmp_path, synthetic_pdf, capsys):
    cfg = _write_config(tmp_path, [{"doc_id": "SYN_C", "path": _rel(synthetic_pdf)}])
    src = _write_sources(tmp_path, [])
    code = run_all_mod.main(["--config", str(cfg), "--sources", str(src), "--data-dir", str(tmp_path / "data")])
    assert code == 0
    assert (tmp_path / "data" / "processed" / "cleaned" / "SYN_C.json").exists()
    assert not (tmp_path / "data" / "processed" / "rag_ready" / "SYN_C.jsonl").exists()
    assert "缺書目" in capsys.readouterr().out


def test_one_failure_does_not_stop_others(run_all_mod, tmp_path, synthetic_pdf, capsys):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"this is not a pdf")
    cfg = _write_config(tmp_path, [{"doc_id": "BROKEN", "path": _rel(broken)},
                                   {"doc_id": "SYN_D", "path": _rel(synthetic_pdf)}])
    src = _write_sources(tmp_path, ["BROKEN", "SYN_D"])
    code = run_all_mod.main(["--config", str(cfg), "--sources", str(src), "--data-dir", str(tmp_path / "data")])
    out = capsys.readouterr().out
    assert code == 1                                   # 有失敗 → 結束碼 1
    assert "失敗  BROKEN" in out and "完成  SYN_D" in out


def test_only_filters_and_rejects_unknown(run_all_mod, tmp_path, synthetic_pdf):
    cfg = _write_config(tmp_path, [{"doc_id": "SYN_E", "path": _rel(synthetic_pdf)}])
    src = _write_sources(tmp_path, ["SYN_E"])
    assert run_all_mod.main(["--config", str(cfg), "--sources", str(src), "--only", "NOPE"]) == 2


def test_config_rejects_duplicates_and_bad_ids(run_all_mod, tmp_path):
    cfg = _write_config(tmp_path, [{"doc_id": "X", "path": "a.pdf"}, {"doc_id": "X", "path": "b.pdf"}])
    with pytest.raises(ValueError):
        run_all_mod.load_config(cfg)
    cfg = _write_config(tmp_path, [{"doc_id": "中文 ID", "path": "a.pdf"}])
    with pytest.raises(Exception):
        run_all_mod.load_config(cfg)


def test_project_config_is_valid(run_all_mod):
    entries = run_all_mod.load_config(ROOT / "config" / "documents.json")
    assert {"TH013", "TH014", "AR015"} <= {e.doc_id for e in entries}
    sources = run_all_mod.registered_sources(ROOT / "config" / "sources.json")
    assert {e.doc_id for e in entries} <= sources       # 登錄的文獻都有書目
