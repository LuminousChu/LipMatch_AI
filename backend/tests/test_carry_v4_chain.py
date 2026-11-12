"""carry-v4（2026-11-07）：接續題要接上一則「實際送問答的整串」，不是只接它的原句。

例：「明天有約會…該擦什麼顏色」→「不要太亮」→ 系統補問 →「是的」，
第三輪舊版只接「不要太亮；是的」，丟了約會與要擦什麼顏色，回答變成來源不足。
這裡用開發句模擬「推薦需求 → 改條件 → 再改條件」，看第三輪送問答的查詢仍包含第一輪的需求；
沒有接續過的上一則照舊用原句（P04 第二輪等既有行為不變）；整串最多保留最後 4 段。
"""
from __future__ import annotations

from app import main as M

from .test_route_v1 import chat, seen_queries  # noqa: F401  （共用 fixture）

ASK = "明天有約會 想要清爽亮麗一點 該擦什麼顏色"   # SYNTHETIC 開發句


def test_third_turn_keeps_first_need(chat, seen_queries):  # noqa: F811
    say, detail = chat
    say(ASK)
    second = say("不要太亮")
    third = say("想要自然一點")
    assert detail(second)["route"].get("carry_over"), "第二輪應該是接續題（推薦需求後改條件）"
    q3 = detail(third)["route"]["qa_question"]
    assert q3.startswith(ASK) and q3.endswith("想要自然一點") and "不要太亮" in q3


def test_previous_base_uses_raw_question_when_not_carried():
    prev = {"question": "我是冷底，想擦紅色", "detail": {"route": {"qa_question": "我是冷底，想擦紅色"}}}
    assert M._previous_base(prev) == "我是冷底，想擦紅色"


def test_previous_base_uses_chain_and_caps_segments():
    chain = "；".join(f"第{i}段" for i in range(1, 7))
    prev = {"question": "第6段", "detail": {"route": {"qa_question": chain,
                                                      "carry_over": {"method": "join_previous_question"}}}}
    assert M._previous_base(prev) == "第3段；第4段；第5段；第6段"


def test_previous_base_prefers_retrieval_question_over_direction_sentence():
    prev = {"question": "哪個好", "detail": {"route": {
        "qa_question": "A；上一輪給的方向：第一個「藍調紅」；哪個好",
        "carry_over": {"method": "previous_directions", "retrieval_question": "A；哪個好"}}}}
    assert M._previous_base(prev) == "A；哪個好"
