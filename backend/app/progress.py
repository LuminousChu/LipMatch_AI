"""progress-hint-v1（2026-11-08：讓使用者送出問題後第 1 秒就有反應）。

整支在做什麼：
  送出問題時，前端帶一個自己產生的隨機權杖（X-Progress-Token 標頭）。後端處理這則提問的過程中，
  把「現在做到哪一步」與「這次參考的來源標題」暫存在記憶體；前端每秒用 GET /api/progress/{權杖} 查詢，
  在等待畫面顯示進度。回答內容、檢查流程、日誌寫法都不變——這裡只多了「進度」這份暫存。

規範對照：
  - 規範 1：每筆進度記著發問者的 user_id，只有本人查得到；別人或沒有權杖一律查不到（回 None，路由回 404）。
  - 規範 2：來源標題取自本次已通過角色權限與核准檢查、實際要送進模型的片段（只放文件標題，不放原文、不放模型內容）。
  - 規範 8：進度不是正式紀錄，不寫資料庫；正式紀錄照舊由 requests 表負責。
  - 記憶體暫存：後端是單一 worker（backend/Dockerfile），重啟就清空；過期（10 分鐘）或超過上限的舊項目會被清掉。

用法（qa.py、main.py）：
  with progress.tracking(token, user_id):   # main.post_message 外層
      ...
      progress.report("retrieved", sources=[...])   # 任何深度的函式都能回報，不用改參數
"""
from __future__ import annotations

import contextvars
import re
import threading
import time
from contextlib import contextmanager
from typing import Iterator, Sequence

PROGRESS_VERSION = "progress-hint-v1"
HEADER = "X-Progress-Token"
# 權杖：前端用 crypto.randomUUID() 產生（36 字含連字號）；只接受這種格式，其他一律不追蹤
_TOKEN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TTL_S = 600                 # 10 分鐘沒更新就清掉
MAX_ENTRIES = 500           # 超過就先清最舊的（防止記憶體一直長）
MAX_SOURCES = 5             # 畫面最多列 5 個來源標題

# 階段代碼 → 給使用者看的一句話（前端直接顯示，不另外翻譯）
STAGES = {
    "received": "已收到問題，正在理解需求…",
    "retrieving": "正在查找相關的文獻來源…",
    "retrieved": "已找到相關來源，正在整理回答…",
    "generating": "正在根據來源撰寫回答…",
    "checking": "正在逐句核對回答與來源…",
    "done": "回答已完成。",
}

_lock = threading.Lock()
_store: dict[str, dict] = {}
_current: contextvars.ContextVar[str | None] = contextvars.ContextVar("progress_token", default=None)


def valid_token(token: str | None) -> bool:
    """權杖格式是否合法（只接受前端 randomUUID 的格式）。"""
    return bool(token) and bool(_TOKEN.match(token))


def _prune(now: float) -> None:
    """清掉過期項目；還是太多就從最舊的開始刪。呼叫端要先拿鎖。"""
    for k in [k for k, v in _store.items() if now - v["updated"] > TTL_S]:
        del _store[k]
    if len(_store) > MAX_ENTRIES:
        for k, _ in sorted(_store.items(), key=lambda kv: kv[1]["updated"])[:len(_store) - MAX_ENTRIES]:
            del _store[k]


@contextmanager
def tracking(token: str | None, user_id: object) -> Iterator[None]:
    """在這個區塊內，report() 都記到這個權杖；權杖不合法就什麼都不做（照常回答）。
    區塊結束（不論成功或例外）標成 done，前端最後一次查詢會看到完成。"""
    if not valid_token(token):
        yield
        return
    now = time.time()
    with _lock:
        _prune(now)
        _store[token] = {"user_id": str(user_id), "stage": "received", "sources": [], "started": now, "updated": now}
    ctx = _current.set(token)
    try:
        yield
    finally:
        _current.reset(ctx)
        report_to(token, "done")


def report(stage: str, sources: Sequence[str] | None = None) -> None:
    """回報目前這則提問做到哪一步；不在 tracking 區塊內（例如離線腳本、測試）就什麼都不做。"""
    token = _current.get()
    if token:
        report_to(token, stage, sources)


def report_to(token: str, stage: str, sources: Sequence[str] | None = None) -> None:
    """更新指定權杖的階段；sources 有給才更新（去重、去空白、最多 MAX_SOURCES 個）。未知階段不記。"""
    if stage not in STAGES:
        return
    with _lock:
        item = _store.get(token)
        if item is None:
            return
        item["stage"], item["updated"] = stage, time.time()
        if sources is not None:
            clean = [s.strip() for s in sources if isinstance(s, str) and s.strip()]
            item["sources"] = list(dict.fromkeys(clean))[:MAX_SOURCES]


def view(token: str, user_id: object) -> dict | None:
    """給前端的進度；權杖不存在、過期或不是本人的就回 None（路由一律回 404，不透露存在與否）。"""
    if not valid_token(token):
        return None
    now = time.time()
    with _lock:
        _prune(now)
        item = _store.get(token)
        if item is None or item["user_id"] != str(user_id):
            return None
        return {"stage": item["stage"], "label": STAGES[item["stage"]], "sources": list(item["sources"]),
                "elapsed_s": round(now - item["started"], 1), "version": PROGRESS_VERSION}


def _reset_for_tests() -> None:
    """測試用：清空暫存。"""
    with _lock:
        _store.clear()
# 核心邏輯：
# 1. 權杖由前端產生、格式固定；後端只把它當成暫存的鑰匙，不寫資料庫、不進日誌。
# 2. contextvars 讓 qa.py 深處的函式不用改參數就能回報；FastAPI 同步路由在執行緒裡跑，每個請求各自一份。
# 3. 查詢一定比對 user_id，別人的權杖等同不存在（規範 1）。
