"""修復 A／D20：manifest.source_path 不綁本機絕對路徑。

程式資料夾沒有 data/raw、原始檔在另一個資料夾時，01_extract 以前寫入絕對路徑；
backend ingest 以 --root 換資料夾解包後就判定「路徑超出資料根目錄」。設 LIPMATCH_DATA_ROOT 後改記相對路徑。
"""

from __future__ import annotations

from pathlib import Path


def test_rel_posix_outside_root_without_data_root_stays_absolute(extract_mod, tmp_path, monkeypatch):
    monkeypatch.delenv("LIPMATCH_DATA_ROOT", raising=False)
    f = tmp_path / "data" / "raw" / "web" / "X.html"
    f.parent.mkdir(parents=True)
    f.write_text("x", encoding="utf-8")
    # 修前行為（也是沒設資料根目錄時的行為）：絕對路徑
    assert Path(extract_mod.rel_posix(f)).is_absolute()


def test_rel_posix_uses_data_root(extract_mod, tmp_path, monkeypatch):
    f = tmp_path / "data" / "raw" / "web" / "X.html"
    f.parent.mkdir(parents=True)
    f.write_text("x", encoding="utf-8")
    monkeypatch.setenv("LIPMATCH_DATA_ROOT", str(tmp_path))
    assert extract_mod.rel_posix(f) == "data/raw/web/X.html"


def test_rel_posix_prefers_code_root(extract_mod, monkeypatch, tmp_path):
    monkeypatch.setenv("LIPMATCH_DATA_ROOT", str(tmp_path))
    here = Path(extract_mod.__file__).resolve()
    assert extract_mod.rel_posix(here) == "scripts/01_extract.py"


def test_html_scope_checker_finds_raw_under_data_root(clean_mod, tmp_path, monkeypatch):
    """相對 source_path 在程式根目錄找不到時，要改到 LIPMATCH_DATA_ROOT 找原 HTML；
    否則 PR015 章節範圍修復（cd1a1b3）在程式與資料分開放時會靜默退回舊行為（section_scope_unchecked）。"""
    import hashlib
    from types import SimpleNamespace

    raw = b"<html><body><main><h1>T</h1><p>x</p></main></body></html>"
    f = tmp_path / "data" / "raw" / "web" / "Z_repair_a_probe.html"
    f.parent.mkdir(parents=True)
    f.write_bytes(raw)
    manifest = SimpleNamespace(source_path="data/raw/web/Z_repair_a_probe.html", sha256=hashlib.sha256(raw).hexdigest())
    monkeypatch.delenv("LIPMATCH_DATA_ROOT", raising=False)
    checker, issue = clean_mod.html_scope_checker(manifest)
    assert checker is None and issue.issue_type == "section_scope_unchecked"  # 修前：找不到
    monkeypatch.setenv("LIPMATCH_DATA_ROOT", str(tmp_path))
    checker, issue = clean_mod.html_scope_checker(manifest)
    assert checker is not None and issue is None
