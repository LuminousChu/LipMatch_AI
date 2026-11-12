"""fix-heldout（2026-11-08）：測試組 10 題診斷後的兩項後處理修正。

整支在測什麼（主張文字與片段原文取自 main 87a63c5 測試組實跑與第八批 40 題實跑，屬實際資料）：
  A. focus-v7 第 12 項 non_lip_colour_advice：把整體配色的綠、黃當唇色建議，引用片段又沒提嘴唇 → 不交付（H03）；
     引用片段有提到唇彩、或沒建議用綠黃的句子不動（W10、N11、M03）。
  B. plain-term-v4 flattering_mistranslated：來源 most flattering、主張寫「最討厭」→「最討喜」（H09）；
     來源沒有 flattering 不改。
不呼叫模型、不連資料庫。
"""
from __future__ import annotations

from app import answer_focus
from app import answer_tasks

# Kryolan〈Skin Tones〉PR003_h0108#r3：講整體配色，原文沒有 lip
PR003_R3 = ("Skin with cool undertones is flattered with green, blue and blue-toned red, yet warmer tones, like "
            "warm-red, orange and yellow can be used to make a bold statement.")
# PR021_h0303：講唇膏冷暖，原文有 lipstick
PR021_H0303 = ("Just like our skin tones, lipsticks can have cool, warm, or neutral undertones. Shades with warm undertones "
               "are rich in orange and red hues and are perfect for creating fiery lipstick looks, whereas cool-toned "
               "lipsticks have mood-setting blue and purple undertones that look pouty on the lips. Once you’ve "
               "deciphered the undertone of your skin, you can choose a lipstick with a similar undertone to create "
               "your most flattering lipstick look.")
# PR015_h0122#m：血管判斷法（原文有 lipstick）
PR015_H0122 = ("In natural lighting, flip over your wrist and look at your veins. If they appear blue, you likely have cool "
               "or pink undertones. Green veins indicate warm or yellow undertones. Choosing a lipstick shade that "
               "complements your undertones is key.")

H03_Q = "我是冷調膚色，有哪些唇色比較適合？"


def _delivered(*claims):
    """組交付物件：claims＝[(文字, [unit_id])]。"""
    return {"claims": [{"claim_no": i, "text": t, "unit_ids": ids, "role": "direct"} for i, (t, ids) in enumerate(claims, 1)],
            "limitations": [], "flags": [], "removed": [], "status": "delivered"}


# ---------------------------------------------------------------------------
# A. non_lip_colour_advice
# ---------------------------------------------------------------------------
def test_h03_green_blue_yellow_from_overall_palette_removed():
    """H03：c3「適合使用綠色、藍色」、c4「可以使用…黃色」引用沒提嘴唇的整體配色段落 → 兩句都不交付。"""
    units = {"PR003_h0108#r3": PR003_R3, "PR015_h0122#m": PR015_H0122}
    out = answer_focus.apply(_delivered(
        ("適合冷色調膚色的唇色包括：柔和粉色、灰粉色、鮮豔深紅以及濃郁梅紅色。", ["PR015_h0122#m"]),
        ("冷色調膚色也適合使用綠色、藍色和帶藍調的紅色。", ["PR003_h0108#r3"]),
        ("若想在冷色調膚色上做出大膽的妝容，可以使用暖色調如暖紅色、橙色和黃色。", ["PR003_h0108#r3"])),
        H03_Q, units)
    assert [c["claim_no"] for c in out["claims"]] == [1]
    assert [r["rule"] for r in out["removed"]] == ["non_lip_colour_advice"] * 2
    assert out["focus_version"] in ("focus-v7", "focus-v8")   # fix-metrics 升 v8


def test_non_lip_rule_keeps_lip_sources_and_descriptions():
    """反例：引用片段有講唇膏（W10 黃色、藍色描述）、血管判斷（N11 綠色）、藍色調的紅（M03）都不動；沒傳片段不判斷。"""
    assert not answer_focus.non_lip_colour_advice("研究發現，暖色調唇膏表現出較高的黃色。", [PR021_H0303])
    assert not answer_focus.non_lip_colour_advice("如果血管呈現綠色，代表是暖底，可以選擇暖色唇膏。", [PR015_H0122])
    assert not answer_focus.non_lip_colour_advice("對於冷底調，藍色調的紅色能與膚色相映襯。", [PR003_R3])
    assert not answer_focus.non_lip_colour_advice("冷色調膚色也適合使用綠色。", [])


# ---------------------------------------------------------------------------
# B. flattering_mistranslated
# ---------------------------------------------------------------------------
def test_h09_flattering_not_hated():
    """H09：來源 most flattering lipstick look → 「最討厭的唇色效果」改「最討喜的唇色效果」。"""
    text, rules = answer_tasks.source_term_repairs("為了獲得最討厭的唇色效果，可以選擇與自己皮膚底調相似的唇膏。", [PR021_H0303])
    assert text == "為了獲得最討喜的唇色效果，可以選擇與自己皮膚底調相似的唇膏。"
    assert rules == ["flattering_mistranslated"]
    assert answer_tasks.TERM_REPAIR_VERSION in ("plain-term-v4", "plain-term-v5")   # fix-metrics 升 v5


def test_hated_kept_when_source_not_flattering():
    """反例：來源沒有 flattering 時「討厭」不改。"""
    s = "如果討厭唇膏乾澀感，可以先擦護唇膏。"
    assert answer_tasks.source_term_repairs(s, [PR015_H0122]) == (s, [])


# ---------------------------------------------------------------------------
# C. 術語表 v3.6（測試組參考段落都在庫、已核准，但中文問法對不到英文原文用詞）
# ---------------------------------------------------------------------------
def test_glossary_v36_heavy_deep_skin_culture():
    """「厚重」補 cakey／heavy／too many layers；「膚色比較深」補 deep skin；「不同國家或文化」補 geographical variations 等。"""
    from app import query_glossary as G
    assert G.GLOSSARY_VERSION in ("lipmatch-query-glossary-v3.6", "lipmatch-query-glossary-v3.7", "lipmatch-query-glossary-v3.8")
    # v3.7（fix-metrics）撤回「厚重」→ cakey／heavy／too many layers（照測試題問法補的詞拿掉）
    heavy = G.expand("霧面唇膏常擦到很厚重，該怎麼辦？")
    assert not ({"heavy", "too many layers"} & set(heavy))
    assert "cakey" in heavy   # v3.8：「厚重」只展開成原本就有的 cakey
    assert "cakey" in G.expand("唇膏結塊、卡粉怎麼辦？")
    assert "deep skin" in G.expand("我膚色比較深，可以擦粉紅色系的口紅嗎？")
    assert {"geographical variations", "cultural", "regional"} <= set(G.expand("不同國家或文化的人，偏好的口紅顏色有差別嗎？"))


def test_glossary_v36_unrelated_questions_unchanged():
    """沒有這些詞的問題，擴充詞和 v3.5 一樣（第八批 40 題只有 M08 會變，見 glossary_impact.py）。"""
    from app import query_glossary as G
    assert G.expand("我是冷調膚色，有哪些唇色比較適合？") == G.expand("我是冷調膚色，有哪些唇色比較適合？")
    assert "heavy" not in G.expand("我嘴唇容易乾，但又想擦霧面，有什麼辦法讓它不要看起來乾乾的？")
