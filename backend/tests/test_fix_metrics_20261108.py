"""fix-metrics（2026-11-08）：plain-term-v5，撲定妝粉步驟的面紙句（測試組 H07 引用完整性差門檻 0.006 的原因）。

整支在測什麼（主張文字取自 fix-heldout 重跑的模型輸出、來源句節錄自 PR015_h0146#m，屬實際資料）：
  1. 正例：主張講定妝粉＋「面紙輕壓吸掉多餘的唇膏」，來源是 press a tissue over your lips → 改「把面紙輕壓在嘴唇上」。
  2. 反例：護唇膏的 blot（吸掉多餘護唇膏）不動；沒講定妝粉不動；來源沒有 press a tissue over lips 不動。
不呼叫模型、不連資料庫。
"""
from __future__ import annotations

from app import answer_tasks as A

SRC = ("Use setting powder for ultra-long wear: After applying your lipstick, lightly press a tissue over your lips "
       "and dust a translucent setting powder, like the L'Oréal Paris Infallible Blur-Fection Longwear Loose Setting "
       "Powder, over the tissue.")
H07 = "在塗抹唇膏後，先用面紙輕壓吸掉多餘的唇膏，再用透明定妝粉輕輕壓在面紙上，這有助於固定色素且不會讓顏色變暗，讓霧面妝效更持久。"


def test_h07_powder_tissue_sentence_fixed():
    """H07：來源是墊面紙撲粉，不是吸唇膏 → 改成「把面紙輕壓在嘴唇上」，其餘文字不動。"""
    text, rules = A.source_term_repairs(H07, [SRC])
    assert text == "在塗抹唇膏後，先把面紙輕壓在嘴唇上，再用透明定妝粉輕輕壓在面紙上，這有助於固定色素且不會讓顏色變暗，讓霧面妝效更持久。"
    assert rules == ["tissue_over_lips_for_powder"]
    assert A.TERM_REPAIR_VERSION == "plain-term-v5"


def test_balm_blot_and_other_cases_unchanged():
    """反例：護唇膏的 blot、沒講定妝粉、來源沒有 press a tissue over lips，都不改。"""
    balm = "在擦唇膏前，先塗上一層薄薄的護唇膏，並用面紙輕壓吸掉多餘的護唇膏，再塗上唇膏。"
    assert A.source_term_repairs(balm, [SRC + " Apply a thin layer of lip balm, blot away the excess."]) == (balm, [])
    no_powder = "塗抹唇膏後，用面紙輕壓吸掉多餘的唇膏，這有助於讓唇妝更持久。"
    assert A.source_term_repairs(no_powder, [SRC]) == (no_powder, [])
    assert A.source_term_repairs(H07, ["Blot your lips with a tissue, then dust powder."]) == (H07, [])


# ---------------------------------------------------------------------------
# focus-v8 第 13 項：粉底的說法套成唇色（驗證組 P02；來源句節錄自 PR003_h0105#r2、PR030_h0053）
# ---------------------------------------------------------------------------
from app import answer_focus as F  # noqa: E402

FOUNDATION_SRC = ("So, if you use a foundation that's too warm or cool on a neutral undertone, it can look orange or "
                  "pink. Likewise, if you use a color that's warm on a cool undertone, or cool on a warm undertone, it can appear grey.")
ORANGE_SRC = ("Orange lipstick comes in both warm and cool undertones. If you have warm undertones, opt for warmer "
              "oranges – and for cooler undertones, cooler shades will complement your complexion beautifully.")


def _delivered(*claims):
    """測試用交付物件：claims＝[(文字, [unit_id])]。"""
    return {"claims": [{"claim_no": i, "text": t, "unit_ids": ids, "role": "direct"} for i, (t, ids) in enumerate(claims, 1)],
            "limitations": [], "flags": [], "removed": [], "status": "answered"}


def test_p02_foundation_sentence_dropped():
    """P02：引用粉底段落（沒提嘴唇）卻寫成唇色的那句不交付；引用橘色口紅段落的那句照留。"""
    d = _delivered(("對於冷底調，選擇偏向粉紅色或褐色調的冷色系橘色，會與您的膚色完美搭配。", ["PR030_h0053"]),
                   ("若在冷底調上使用過於暖色的唇色，可能會顯得灰暗。", ["PR003_h0105#r2"]))
    out = F.apply(d, "我是冷底，可是很想擦橘色系，有辦法擦得比較協調嗎？",
                  {"PR030_h0053": ORANGE_SRC, "PR003_h0105#r2": FOUNDATION_SRC})
    assert [c["claim_no"] for c in out["claims"]] == [1]
    assert [r["rule"] for r in out["removed"] if r.get("rule") == "foundation_advice_as_lip"] == ["foundation_advice_as_lip"]
    assert out["focus_version"] == "focus-v8"


def test_foundation_rule_negative_cases():
    """反例：來源有提到 lip 不動；主張沒講唇色不動；來源沒講粉底不動；沒傳片段不動。"""
    assert not F.foundation_advice_as_lip("唇色選冷調。", [FOUNDATION_SRC + " Pair with a cool lip."])
    assert not F.foundation_advice_as_lip("冷底擦太暖的粉底會顯灰。", [FOUNDATION_SRC])
    assert not F.foundation_advice_as_lip("唇色選冷調。", ["Cool undertones suit blue and green."])
    assert not F.foundation_advice_as_lip("唇色選冷調。", [])
