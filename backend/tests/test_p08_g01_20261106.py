"""2026-11-05 審核 40 題後的待修：insufficient-lead-v1（P08）、exit-assist-v2（LM35-G01）。

P08 的限制句取自 T4 跑測 4d1ef36 的實際輸出（data/review/route-v4-compare-20261101/ai.json）；
其他問句是為了檢查規則邊界自寫的，不是題庫題目。
"""
from __future__ import annotations

import pytest

from app import exit_assist as E

P08_LIMITS = [
    "證據中沒有關於「藍調紅」與「奶茶色」對面試官專業印象百分點影響的數據。",
    "證據中沒有關於「奶茶色」唇彩對皮膚視覺效果的具體研究數據。",
    "想再確認：請問您提到的「奶茶色」是指特定的產品色號，還是指一般的裸色系？",
]


# ---------------------------------------------------------------- insufficient-lead-v1（P08）
def test_p08_lead_is_first_missing_limitation():
    answer, rest, lead = E.insufficient_general_answer(P08_LIMITS, "一般建議第一句。〔來源 4〕")
    assert lead == P08_LIMITS[0]
    lines = answer.split("\n")
    assert lines[0] == E.INSUFFICIENT_LEAD_PREFIX + P08_LIMITS[0]          # 一開頭就說缺什麼
    assert lines[1] == E.INSUFFICIENT_ASSIST_HEAD                           # 再接一般建議標題
    assert lines[2] == "一般建議第一句。〔來源 4〕"
    # insufficient-lead-v2（2026-11-08）：其餘「缺什麼」旁枝不再列，補問句照舊
    assert rest == [x for x in P08_LIMITS[1:] if x.endswith("？") or x.startswith("想再確認")]


@pytest.mark.parametrize("limits, expected", [
    # 補問句不拿來當開頭
    (["想再確認：您說的奶茶色是哪一種？", "證據未提供樣本條件。"], "證據未提供樣本條件。"),
    # 問號結尾也算補問
    (["證據沒有說明是哪一種紅色？", "證據缺少面試情境的研究。"], "證據缺少面試情境的研究。"),
    # 帶來源標記的不拿
    (["證據中沒有比較數據〔來源 2〕。", "來源未涵蓋面試官評價。"], "來源未涵蓋面試官評價。"),
    # 系統固定句（不回答、不用模型記憶）不拿：後面接著一般建議，會互相矛盾
    (["沒有找到與問題相關、且你可以使用的已核准來源片段，這次不回答，也不會用模型記憶補充。"], None),
    # 不是在說缺什麼的限制不拿
    (["以上建議以日常情境為準。"], None),
    ([], None),
])
def test_missing_lead_rules(limits, expected):
    assert E.missing_lead(limits) == expected


def test_no_lead_keeps_old_shape():
    """挑不到開頭句時，回答與 insufficient-assist-v1 相同，限制全部保留。"""
    limits = ["想再確認：請問是哪個場合？"]
    answer, rest, lead = E.insufficient_general_answer(limits, "一般建議。")
    assert lead is None and rest == limits
    assert answer == f"{E.INSUFFICIENT_ASSIST_HEAD}\n一般建議。"


# ---------------------------------------------------------------- exit-assist-v2（G01）
@pytest.mark.parametrize("q, expected", [
    # 沒有顏色詞：維持原本的問法
    ("我是冷底，想找上班用的口紅，請直接推薦哪一支，列出品牌和完整色號。",
     "我是冷底，想找上班用的口紅，適合往哪個色系方向找？"),
    # 其他色系詞也適用（不是只認紅色）
    ("我是暖底，想找約會用的豆沙色唇膏，請列出品牌和色號。",
     "我是暖底，想找約會用的豆沙色唇膏，適合哪一種豆沙色？"),
    ("我是黃皮，想找粉色系口紅，請推薦哪一支。",
     "我是黃皮，想找粉色系口紅，適合哪一種粉色？"),
    # 「口紅」「紅潤」不算顏色詞
    ("我想看起來紅潤有氣色，推薦哪一支。",
     "我想看起來紅潤有氣色，適合往哪個色系方向找？"),
    # 否定或程度語氣的子句不拿來當想找的顏色
    ("我是冷底，不要太紅的口紅，請推薦哪一支。",
     "我是冷底，不要太紅的口紅，適合往哪個色系方向找？"),
])
def test_in_scope_question_color_tail(q, expected):
    assert E.in_scope_question(q, "product_not_enabled", []) == expected


def test_wanted_colors_order_and_limit():
    assert E.wanted_colors(["想找紅色或豆沙色或紫色"]) == ["紅色", "豆沙色"]


# ---------------------------------------------------------------- echo-paren-v1（G01 重複）
from app import postprocess as PP   # noqa: E402


@pytest.mark.parametrize("text, expected, removed", [
    # T4 11-06 G01 實際輸出
    ("如果是冷底調，也可以選擇深紫紅色（深紫紅色）。", "如果是冷底調，也可以選擇深紫紅色。", ["（深紫紅色）"]),
    ("可以選 MLBB(MLBB) 色。", "可以選 MLBB 色。", ["(MLBB)"]),
    # 翻譯、補充說明不是照抄，保留
    ("建議選擇鮮豔的深紅色（vibrant crimsons）。", "建議選擇鮮豔的深紅色（vibrant crimsons）。", []),
    ("選接近唇色的顏色（即 MLBB）。", "選接近唇色的顏色（即 MLBB）。", []),
    # 括號內容是前面字的結尾（深紫紅色 以 紫紅色 結尾）也算照抄
    ("深紫紅色（紫紅色）也可以。", "深紫紅色也可以。", ["（紫紅色）"]),
])
def test_drop_echo_paren(text, expected, removed):
    assert PP.drop_echo_paren(text) == (expected, removed)
