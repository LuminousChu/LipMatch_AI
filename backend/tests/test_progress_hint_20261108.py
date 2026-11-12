"""progress-hint-v1（2026-11-08）：等待回答時的進度與來源標題。

整支在測什麼：
  1. 進度暫存（app/progress.py）：tracking 區塊內 report 記到同一個權杖、區塊結束標 done；
     權杖格式不對就不追蹤；來源標題去重、去空白、最多 5 個；未知階段不記；過期會被清掉。
  2. 規範 1：只有發問者本人查得到；別人、未登入、亂打的權杖一律 404／401。
  3. 提問 API：帶 X-Progress-Token 送出問題，回答內容和不帶時相同（只多了進度）；送完查得到 done。
  4. 回答流程（qa._answer_repair）在 tracking 區塊內會依序回報 retrieving → retrieved（附標題）→ generating → checking。
文件內容沿用 test_ingest 的 SYNTHETIC 合成資料；不呼叫模型。
"""
from __future__ import annotations

import time
import uuid

import pytest

from app import progress

from .conftest import login
from .test_ui_api import _conv

TOKEN = "3f2b8c1e-9a4d-4e6f-8b2a-1c3d5e7f9a0b"


@pytest.fixture(autouse=True)
def _clean():
    """每個測試前後清空暫存，避免互相影響。"""
    progress._reset_for_tests()
    yield
    progress._reset_for_tests()


# ---------------------------------------------------------------------------
# 1. 進度暫存
# ---------------------------------------------------------------------------
def test_tracking_reports_and_finishes():
    """區塊內 report 會更新階段與來源；區塊結束後是 done，來源保留。"""
    with progress.tracking(TOKEN, 7):
        assert progress.view(TOKEN, 7)["stage"] == "received"
        progress.report("retrieved", sources=["〈A〉", " A2 ", "", "A2", "B", "C", "D", "E", "F"])
        v = progress.view(TOKEN, 7)
        assert v["stage"] == "retrieved" and v["label"] == progress.STAGES["retrieved"]
        assert v["sources"] == ["〈A〉", "A2", "B", "C", "D"]          # 去空白、去重、最多 5 個
        progress.report("not-a-stage")                                   # 未知階段不記
        assert progress.view(TOKEN, 7)["stage"] == "retrieved"
    done = progress.view(TOKEN, 7)
    assert done["stage"] == "done" and done["sources"][0] == "〈A〉"


def test_done_even_when_exception():
    """處理中出錯也會標 done（前端不會一直顯示處理中）。"""
    with pytest.raises(RuntimeError):
        with progress.tracking(TOKEN, 7):
            raise RuntimeError("boom")
    assert progress.view(TOKEN, 7)["stage"] == "done"


def test_invalid_token_not_tracked_and_report_outside_is_noop():
    """權杖格式不對：不追蹤、照常執行；不在區塊內 report 什麼都不做。"""
    with progress.tracking("../../etc/passwd", 7):
        progress.report("generating")
    assert progress.view("../../etc/passwd", 7) is None
    progress.report("generating")                    # 區塊外（離線腳本、測試）不出錯
    assert progress._store == {}


def test_owner_only_and_expiry(monkeypatch):
    """別人的權杖查不到；超過 TTL 沒更新就被清掉。"""
    with progress.tracking(TOKEN, 7):
        assert progress.view(TOKEN, 8) is None
    real = time.time()
    monkeypatch.setattr(progress.time, "time", lambda: real + progress.TTL_S + 5)
    assert progress.view(TOKEN, 7) is None


# ---------------------------------------------------------------------------
# 2、3. API
# ---------------------------------------------------------------------------
def test_progress_api_owner_only(client, users):
    """帶權杖提問 → 本人查得到 done；別人 404、未登入 401、亂打的權杖 404。回答和不帶權杖時相同。"""
    alice, bob = login(client, "alice"), login(client, "bob")
    cid = _conv(client, alice)
    plain = client.post(f"/api/conversations/{cid}/messages", headers=alice, json={"question": "上班用的低調唇色"})
    tracked = client.post(f"/api/conversations/{cid}/messages", headers={**alice, progress.HEADER: TOKEN},
                          json={"question": "上班用的低調唇色"})
    assert plain.status_code == tracked.status_code == 201
    keys = ("status", "error_code", "answer", "message", "limitations", "recommendation")
    assert {k: plain.json().get(k) for k in keys} == {k: tracked.json().get(k) for k in keys}
    r = client.get(f"/api/progress/{TOKEN}", headers=alice)
    assert r.status_code == 200 and r.json()["stage"] == "done" and r.json()["version"] == progress.PROGRESS_VERSION
    assert client.get(f"/api/progress/{TOKEN}", headers=bob).status_code == 404
    assert client.get(f"/api/progress/{TOKEN}").status_code == 401
    assert client.get(f"/api/progress/{uuid.uuid4()}", headers=alice).status_code == 404
    assert client.get("/api/progress/not-a-token", headers=alice).status_code == 404


# ---------------------------------------------------------------------------
# 4. 回答流程會回報階段
# ---------------------------------------------------------------------------
def test_qa_repair_reports_stages(monkeypatch):
    """_answer_repair 內的回報點依序出現（用替身記錄，不跑檢索與模型）。"""
    import inspect

    from app import qa
    src = inspect.getsource(qa._answer_repair)
    order = [src.index(f'progress.report("{s}"') for s in ("retrieving", "retrieved", "generating", "checking")]
    assert order == sorted(order)
    assert 'sources=[chunk_rows[u].get("title")' in src     # 來源只給文件標題，不給原文
