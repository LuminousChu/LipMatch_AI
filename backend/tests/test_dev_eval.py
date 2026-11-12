"""S10 開發題評估工具本身的檢查：跑得完、兩種資料情境都有 20 題、不捏造 Hit@5、暫存資料庫有刪掉。"""
from __future__ import annotations

import psycopg

from app import dev_eval


def test_dev_eval_runs_and_reports_honestly(pg_admin_url):
    meta, summary, results = dev_eval.evaluate(pg_admin_url)
    assert len(results) == 40
    assert {r.environment for r in results} == set(dev_eval.ENVIRONMENTS)
    # 預期證據全部未核對 → Hit@5 分母 0、不給數字
    assert summary["hit_at_5"]["denominator"] == 0 and summary["hit_at_5"]["numerator"] is None
    assert summary["hit_at_5"]["status"] == "未執行"
    assert summary["citation_support"]["numerator"] is None
    # 版本資訊完整（凍結與重現用）
    for key in ("git_commit", "parser", "vocab_version", "rules_version", "prompt_version", "cases_sha256_16"):
        assert meta[key]
    # 正式資料狀態下不會有任何推薦（0 候選）
    assert all(not r.recommendations for r in results if r.environment == "formal_like")
    # 每題至少一個檢查，案例檔沒有打錯檢查名稱
    assert all(r.checks for r in results)
    assert not any("不認得" in c.detail for r in results for c in r.checks)
    with psycopg.connect(pg_admin_url, autocommit=True) as conn:
        left = conn.execute("SELECT count(*) FROM pg_database WHERE datname LIKE 'lipmatch_eval_%'").fetchone()[0]
    assert left == 0


def test_markdown_lists_failures_with_classification(pg_admin_url):
    meta, summary, results = dev_eval.evaluate(pg_admin_url)
    cases = dev_eval.json.loads(dev_eval.CASES_PATH.read_text(encoding="utf-8"))["cases"]
    md = dev_eval.to_markdown(meta, summary, results, cases)
    n_fail = sum(1 for r in results for c in r.checks if not c.passed)
    assert f"完整失敗清單（{n_fail} 項）" in md
    assert "Hit@5**：未執行" in md
