"""focus-v2（2026-11-07）：商品功能沒開時，不交付模型寫的「證據沒有具體色號／品牌／產品名稱」限制。

限制句取自 exp2（f178b76）40 題 T4 跑測的實際回答（data/review/exp2-40q-20261107/ai.json，Git 外），
判讀見 data/review/ragas-exp2-20261107：G01、M07、N08 三句判錯（片段其實有品牌或色號），M08、P09 兩句判對但沒有資訊。
"""
from __future__ import annotations

from app import answer_focus as F
from app import color_reference as CR
from app import recommend

# exp2 實際限制句
G01_LIM = "證據中沒有提供具體的紅色產品名稱或色號。"
M07_LIM = "證據未提及使用者偏好的具體粉紅色或紅色色號。"
N08_LIM = "證據中沒有提到適合「自然妝」的具體唇膏色號或品牌名稱。"
P09_LIM = "證據中沒有提到如何卸除特定品牌或色號的口紅。"
P10_LIM = "證據未提及如何處理吃飯或喝水後的具體補妝步驟。"          # 不是商品類，要留
PRODUCT_NOTE = "商品功能未啟用：只能提供色系／妝感方向，不提供品牌、色號或價格。"   # 系統固定句，要留

M07_Q = "我皮膚很白，擦很濃烈的口紅會不會反而顯得蒼白？如果想擦紅色或粉色要怎麼挑？"
W02_Q = "珊瑚色的色碼是多少？可以直接給我一個 HEX 嗎？"     # 有色名＝可能附參照：交給 color_reference 改寫，不刪
G01_Q = "我是冷底，想找上班用的紅色口紅，請直接推薦哪一支，列出品牌、系列和完整色號。"   # 問色號但沒有色名，不會附參照
W03_Q = "豆沙色跟奶茶色差在哪裡？"            # 有色名、沒問色號（證據不足時仍可能附參照）


def _delivered(limits):
    """最小的 Delivered：一條主張、指定的限制清單。"""
    return {"status": "delivered",
            "claims": [{"claim_no": 1, "text": "粉色和紅色都可以，依妝感選擇。", "unit_ids": ["u1"], "role": "answer"}],
            "limitations": list(limits)}


def test_product_limits_removed_when_product_off():
    """商品功能沒開、沒問色號：三句判錯的與 P09 都不交付，記在 removed；非商品類的限制留著。"""
    out = F.apply(_delivered([G01_LIM, M07_LIM, N08_LIM, P09_LIM, P10_LIM]), M07_Q)
    assert out["limitations"] == [P10_LIM]
    gone = [r for r in out["removed"] if r.get("rule") == "product_specifics_limit"]
    assert [r["limitation"] for r in gone] == [G01_LIM, M07_LIM, N08_LIM, P09_LIM]
    assert out["focus_version"] in ("focus-v2", "focus-v3", "focus-v4", "focus-v5", "focus-v6", "focus-v7", "focus-v8")   # v3～v8（2026-11-08）只加第 6～13 項


def test_system_fixed_sentences_kept():
    """系統固定句（商品功能說明、色碼固定句）有「品牌、色號」字樣也不刪。"""
    out = F.apply(_delivered([PRODUCT_NOTE, CR.REWRITTEN_LIMITATION]), M07_Q)
    assert out["limitations"] == [PRODUCT_NOTE, CR.REWRITTEN_LIMITATION]


def test_color_code_limit_left_for_color_reference():
    """說「沒有色碼／HEX」的限制不在這裡刪：附色名參照時要被改寫成固定說明。"""
    lim = "證據中沒有提供豆沙色的 HEX 色碼或色號。"
    assert not F.is_product_specifics_limit(lim)
    out = F.apply(_delivered([lim]), M07_Q)
    assert out["limitations"] == [lim]


def test_kept_when_question_names_color():
    """問題裡有色名（可能附色名參照）：不刪，限制交給 color_reference 改寫成固定說明。"""
    for q in (W02_Q, W03_Q):
        out = F.apply(_delivered([G01_LIM]), q)
        assert G01_LIM in out["limitations"], q


def test_removed_when_asking_shade_without_color_name():
    """G01：問色號但沒有色名，不會附參照；商品功能說明由系統另給，這句「證據沒有色號」不交付。"""
    out = F.apply(_delivered([G01_LIM, PRODUCT_NOTE]), G01_Q)
    assert out["limitations"] == [PRODUCT_NOTE]


def test_kept_when_product_enabled(monkeypatch):
    """商品功能打開時，「沒有具體色號」是有資訊的限制，不刪。"""
    monkeypatch.setattr(recommend, "PRODUCT_ENABLED", True)
    out = F.apply(_delivered([G01_LIM, N08_LIM]), M07_Q)
    assert out["limitations"] == [G01_LIM, N08_LIM]


def test_plain_product_word_not_matched():
    """只說「產品」（在講做法）不算商品類限制；否定詞和色號隔太遠（跨句號）也不算。"""
    assert not F.is_product_specifics_limit("證據未說明如何使用該產品打底。")
    assert not F.is_product_specifics_limit("證據沒有提到步驟。另外色號請以官網為準。")


def test_color_name_limit_still_added():
    """第 4 項色名提醒照常加；有色名的題第 5 項不動（模型的限制留給 color_reference）。"""
    out = F.apply(_delivered([N08_LIM]), W03_Q)
    assert N08_LIM in out["limitations"]
    assert any("色名是各品牌自己取的" in x for x in out["limitations"])
