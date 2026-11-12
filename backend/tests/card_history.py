"""route-v4 起正式路由不跑規則推薦；測試用的兩個小工具（不是正式程式）。

- rule_card_for_request：用一則已送出請求當下的對話條件與意圖，直接呼叫 rec.run 算出規則推薦卡（唯讀）。
  用來繼續測規則推薦本身（排序、硬排除、底調條件），不經過正式路由。
- seed_card_history：把一則已送出的請求改寫成 route-v4 之前的推薦卡紀錄（歷史資料）。
  舊對話裡仍有這種紀錄，歷史檢視、引用查看、回饋名次、來源狀態變動後遮蔽都要繼續正確。
"""
from __future__ import annotations

import uuid

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app import preferences as pref
from app import recommend as rec


def _request_and_state(conn, request_id: str) -> tuple[dict, dict]:
    row = conn.execute(
        """SELECT r.detail AS req_detail, c.* FROM requests r JOIN conversations c ON c.id = r.conversation_id
            WHERE r.id = %s""", (request_id,)).fetchone()
    return row["req_detail"], pref.load_state(row)


def rule_card_for_request(db: str, request_id: str, *, role: str = "user", had_prior: bool = False) -> rec.Decision:
    """這則請求當下的條件與意圖交給規則推薦（不寫任何資料）。"""
    with psycopg.connect(db, row_factory=dict_row) as conn:
        detail, state = _request_and_state(conn, request_id)
        return rec.run(conn, state, role=role, intent=detail["intent"], secondary=detail["secondary_intents"],
                       had_prior=had_prior)


def seed_card_history(db: str, request_id: str, *, role: str = "user", had_prior: bool = False) -> rec.Decision:
    """把請求改寫成舊版（route-v3 以前）出推薦卡的紀錄：detail.recommendation、answer_source、推薦名次表。"""
    with psycopg.connect(db, autocommit=True, row_factory=dict_row) as conn:
        detail, state = _request_and_state(conn, request_id)
        decision = rec.run(conn, state, role=role, intent=detail["intent"], secondary=detail["secondary_intents"],
                           had_prior=had_prior)
        rec.record(conn, uuid.UUID(request_id), decision)
        detail = {k: v for k, v in detail.items() if k != "qa"}
        detail.update(outcome=decision.status, answer_source="rule_template",
                      recommendation={**decision.to_dict(), "role": role, "had_prior": had_prior},
                      route={"version": "route-v3", "use_qa": False, "reason": "card_covers_request",
                             "qa_question": None})
        conn.execute(
            """UPDATE requests SET detail = %s, answer = %s, status = 'succeeded', error_code = NULL,
                      error_detail = NULL WHERE id = %s""",
            (Jsonb(detail), rec.render_text(decision) or None, request_id))
        return decision
