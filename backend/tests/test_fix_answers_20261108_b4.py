"""fix-answers 第五批（交接第四批設計）：prodref 審核後 M09、P10、W10、P04 第二輪的後處理修正。

整支在測什麼（主張文字與片段原文取自最終版 T4 實跑輸出 extract_B，屬實際資料，不是合成）：
  A. plain-term-v2（answer_tasks.source_term_repairs）
     - tissue_plain_wording：來源是 tissue、「組織」後接按壓動作 → 面紙（P10）；來源沒 tissue 不改。
     - cupids_bow_wording：來源是 Cupid's bow、沒 philtrum → 人中改唇峰（M09）；來源有 philtrum 不改。
     - neutral_undertone_listed：來源 cool, warm, or neutral 三類並列 → 分類句補中性（W10）；
       描述句（W04「同時包含暖色調與冷色調」）、來源只有兩類時不改。
  B. focus-v5（answer_focus.apply 帶 unit_texts）
     - other_look_steps：問草莓女孩妝怎麼畫，引用夏日粉色妝片段的腮紅、高光步驟不交付（M09）；
       沒問怎麼畫、沒傳片段時不動。
     - unsupported_comparison：「藍調紅比暖紅更適合搭粉色腮紅」引用片段沒提腮紅 → 不交付＋加限制（P04 第二輪）；
       片段有提到搭配對象就留。
  C. reconcile-v6：問整個妝怎麼畫時，腮紅、潤色保濕乳不降背景（M09）；M04 類（沒問怎麼畫）照舊降。
不呼叫模型、不連資料庫。
"""
from __future__ import annotations

from app import answer_focus
from app import answer_tasks
from app import postprocess as PP

# ---------------------------------------------------------------------------
# 片段原文（最終版 T4 送入模型的片段，節錄必要句子）
# ---------------------------------------------------------------------------
PR015_H0146 = ("Use setting powder for ultra-long wear: After applying your lipstick, lightly press a tissue over "
               "your lips and dust a translucent setting powder over the tissue.")
PR017_H0155 = ("This fresh, summery take on the pink makeup trend doesn’t require any eye makeup whatsoever—just a bit "
               "of lip gloss, some highlighter, and plenty of blush. Then, lightly dust the same blush onto the bridge "
               "of your nose and along your hairline to create a sun-kissed feel. Next, highlight the tops of your "
               "cheekbones, Cupid’s bow, and brow bones.")
PR012_H0169 = ("Get the look: While you can achieve strawberry girl makeup with a matte base, we think it looks best "
               "when the skin looks dewy and fresh. As such, we recommend swapping out the full-coverage foundation "
               "for a glowy tinted moisturizer. Then, dab the blush onto the apples of your cheeks, blending the "
               "pigment toward your temples for a subtle sunkissed effect. Finally, complete the look with a tinted "
               "lip gloss or oil.")
PR021_H0303 = ("Just like our skin tones, lipsticks can have cool, warm, or neutral undertones. Shades with warm "
               "undertones are rich in orange and red hues.")
PR003_H0108 = ("Skin with cool undertones is flattered with green, blue and blue-toned red, yet warmer tones, like "
               "warm-red, orange and yellow can be used to make a bold statement.")
PR012_H0156 = "Match your blush to your lipstick to streamline your morning makeup routine."

M09_Q = "草莓女孩妝適合什麼場合？要怎麼畫，唇妝用什麼？"
P04_Q2 = "你剛剛給的第一個和第二個方向，哪個比較好搭我已經有的粉色腮紅？"


def delivered(*claims):
    """組交付物件：claims＝[(文字, [unit_id])]，claim_no 依序編號。"""
    return {"claims": [{"claim_no": i, "text": t, "unit_ids": ids, "role": "direct"} for i, (t, ids) in enumerate(claims, 1)],
            "limitations": [], "flags": [], "removed": [], "status": "answered"}


# ---------------------------------------------------------------------------
# A. plain-term-v2
# ---------------------------------------------------------------------------
def test_p10_tissue_becomes_facial_tissue():
    """P10：來源 press a tissue → 「用組織輕輕壓在嘴唇上」改「用面紙輕輕壓在嘴唇上」。"""
    text, rules = answer_tasks.source_term_repairs(
        "在塗上唇膏後，可以用組織輕輕壓在嘴唇上，並撒上透明定妝粉以幫助固定色素。", [PR015_H0146])
    assert text == "在塗上唇膏後，可以用面紙輕輕壓在嘴唇上，並撒上透明定妝粉以幫助固定色素。"
    assert rules == ["tissue_plain_wording"]


def test_tissue_rule_needs_source_and_press_action():
    """反例：來源沒 tissue 不改；來源有 tissue 但「組織」沒接按壓動作（講皮膚組織）也不改。"""
    s = "可以用組織輕輕壓在嘴唇上。"
    assert answer_tasks.source_term_repairs(s, [PR003_H0108]) == (s, [])
    s2 = "唇部組織比較薄，容易乾燥。"
    assert answer_tasks.source_term_repairs(s2, [PR015_H0146]) == (s2, [])


def test_m09_cupids_bow_becomes_lip_peak():
    """M09：來源 Cupid’s bow（彎引號）→ 人中改唇峰。"""
    text, rules = answer_tasks.source_term_repairs("若想增加光澤感，可以在顴骨上方、人中及眉骨處加上高光。", [PR017_H0155])
    assert text == "若想增加光澤感，可以在顴骨上方、唇峰及眉骨處加上高光。"
    assert rules == ["cupids_bow_wording"]


def test_cupids_bow_rule_skips_when_source_has_philtrum_or_no_cupid():
    """反例：來源同時有 philtrum 不改；來源沒有 Cupid 不改。"""
    s = "在人中處加上高光。"
    assert answer_tasks.source_term_repairs(s, [PR017_H0155 + " Dab a little on the philtrum."]) == (s, [])
    assert answer_tasks.source_term_repairs(s, [PR012_H0169]) == (s, [])


def test_w10_neutral_added_to_classification():
    """W10：來源 cool, warm, or neutral → 「分為冷色調與暖色調」補成三類，原順序不變。"""
    text, rules = answer_tasks.source_term_repairs("唇膏可以分為冷色調與暖色調，這與皮膚底調的分類方式類似。", [PR021_H0303])
    assert text == "唇膏可以分為冷色調、暖色調與中性色調，這與皮膚底調的分類方式類似。"
    assert rules == ["neutral_undertone_listed"]
    text, _ = answer_tasks.source_term_repairs("口紅有暖色調和冷色調兩種。", [PR021_H0303])
    assert text.startswith("口紅有暖色調、冷色調與中性色調")


def test_neutral_rule_negative_cases():
    """反例：描述句（W04）不改；來源只有冷暖兩類不改；主張已有中性不改。"""
    s = "MLBB 唇彩的特徵是中等亮度且低飽和度，且同時包含暖色調與冷色調。"
    assert answer_tasks.source_term_repairs(s, [PR021_H0303]) == (s, [])
    s2 = "唇膏可以分為冷色調與暖色調。"
    assert answer_tasks.source_term_repairs(s2, ["lipsticks can be cool or warm."]) == (s2, [])
    s3 = "唇膏可以分為冷色調與暖色調，也有中性的。"
    assert answer_tasks.source_term_repairs(s3, [PR021_H0303]) == (s3, [])


def test_postprocess_flag_records_term_version():
    """後處理旗標 source_term_corrected 記 term_version＝plain-term-v2；prompt 會用的 VERSION 不變。"""
    import json
    units = {"PR015_h0146#m": {"unit_id": "PR015_h0146#m", "text": PR015_H0146, "conditions": [],
                               "content_role": None, "support_scope": None, "product_mentions": []}}
    raw = json.dumps({"insufficient": False, "claims": [{"text": "可以用組織輕輕壓在嘴唇上。",
                      "unit_ids": ["PR015_h0146#m"], "conditions": []}], "limitations": [],
                      "clarifying_question": None}, ensure_ascii=False)
    out = PP.postprocess(PP.parse_answer(raw), units, PP.Policy(kept_unit_ids=frozenset(units)))
    flag = next(f for f in out["flags"] if f.get("flag") == "source_term_corrected")
    # 第八批升 plain-term-v3（加英文 tissue 規則）；這裡的主張沒有英文 tissue，規則仍只有 tissue_plain_wording
    assert flag["term_version"] == answer_tasks.TERM_REPAIR_VERSION and flag["rules"] == ["tissue_plain_wording"]
    assert out["claims"][0]["text"] == "可以用面紙輕輕壓在嘴唇上。"
    assert answer_tasks.VERSION == "task-focus-v2"


# ---------------------------------------------------------------------------
# B. focus-v5
# ---------------------------------------------------------------------------
M09_UNITS = {"PR012_h0169": PR012_H0169, "PR017_h0155": PR017_H0155}


def m09_delivered():
    """M09 最終版的主張（c6 已經過術語修正：人中→唇峰）。"""
    return delivered(
        ("建議使用帶有光澤的潤色保濕乳代替全妝底妝，以呈現水潤且清新的皮膚質感。", ["PR012_h0169"]),
        ("在臉頰蘋果部位點上粉紅色腮紅，並向太陽穴方向暈開，以營造細微的陽光親吻效果。", ["PR012_h0169"]),
        ("若想營造陽光親吻的感覺，也可以將腮紅輕輕掃在鼻樑和髮際線。", ["PR017_h0155"]),
        ("若想增加光澤感，可以在顴骨上方、唇峰及眉骨處加上高光。", ["PR017_h0155"]),
        ("最後使用帶有色澤的唇蜜或唇油來完成妝容。", ["PR012_h0169"]),
    )


def test_m09_other_look_steps_dropped():
    """M09：引用夏日粉色妝片段（沒有 strawberry）的腮紅、高光步驟不交付；草莓女孩妝片段的步驟照留。"""
    out = answer_focus.apply(m09_delivered(), M09_Q, M09_UNITS)
    assert [c["claim_no"] for c in out["claims"]] == [1, 2, 5]
    gone = [r for r in out["removed"] if r.get("rule") == "other_look_steps"]
    assert [r["claim_no"] for r in gone] == [3, 4]
    assert out["focus_version"] == answer_focus.FOCUS_VERSION   # 第八批升 focus-v6


def test_other_look_steps_negative_cases():
    """反例：沒傳片段（舊呼叫）、沒問怎麼畫、送入片段都沒有那款妝時都不動。"""
    assert len(answer_focus.apply(m09_delivered(), M09_Q)["claims"]) == 5
    assert len(answer_focus.apply(m09_delivered(), "草莓女孩妝適合什麼場合？", M09_UNITS)["claims"]) == 5
    assert len(answer_focus.apply(m09_delivered(), M09_Q, {"PR017_h0155": PR017_H0155})["claims"]) == 5


def p04_delivered():
    """P04 第二輪最終版的四條主張。"""
    return delivered(
        ("選擇藍色調的紅色（blue-toned red）能與冷底調的膚色相映襯。", ["PR003_h0108#r3"]),
        ("若想打造大膽的妝容，冷底調也可以選擇暖色調的紅色（warm-red）。", ["PR003_h0108#r3"]),
        ("為了打造單色妝效，建議將唇膏與腮紅顏色匹配。", ["PR012_h0156"]),
        ("如果是冷底調，選擇藍色調的紅色會比暖色調的紅色更適合與粉色腮紅搭配。", ["PR003_h0108#r3"]),
    )


P04_UNITS = {"PR003_h0108#r3": PR003_H0108, "PR012_h0156": PR012_H0156}


def test_p04_unsupported_comparison_dropped_with_limit():
    """P04 第二輪：比較句引用片段沒提腮紅 → 不交付，另加限制；其他三條照留。"""
    # 第 1 條會被既有的第 2 項（沒提膚色的膚色效果）處理，與這次無關 → 只比對帶不帶片段的差異
    base = answer_focus.apply(p04_delivered(), P04_Q2)
    out = answer_focus.apply(p04_delivered(), P04_Q2, P04_UNITS)
    assert ([c["claim_no"] for c in base["claims"] if c["claim_no"] not in [x["claim_no"] for x in out["claims"]]]
            == [4])
    gone = [r for r in out["removed"] if r.get("rule") == "unsupported_comparison"]
    assert [r["claim_no"] for r in gone] == [4] and gone[0]["object"] == "粉色腮紅"
    assert out["limitations"] == ["來源沒有直接比較哪個方向比較適合搭配粉色腮紅。"]


def test_comparison_kept_when_source_mentions_object():
    """反例：比較句的搭配對象在引用片段裡有（blush）就留；沒傳片段也不動。"""
    d = delivered(("藍調紅比暖紅更適合搭配粉色腮紅。", ["PR012_h0156"]))
    assert len(answer_focus.apply(d, P04_Q2, P04_UNITS)["claims"]) == 1
    assert any(c["claim_no"] == 4 for c in answer_focus.apply(p04_delivered(), P04_Q2)["claims"])


# ---------------------------------------------------------------------------
# C. reconcile-v6
# ---------------------------------------------------------------------------
def test_m09_whole_look_steps_not_background():
    """M09：問「要怎麼畫，唇妝用什麼」，潤色保濕乳、腮紅步驟不降成背景。"""
    out = PP.reconcile(m09_delivered(), M09_Q, list(M09_UNITS.values()))
    assert all(c.get("role") != "background" for c in out["claims"])
    assert not any(f.get("flag") == "off_target_claim" for f in out["flags"])
    assert out["reconcile_version"] in ("reconcile-v6", "reconcile-v7", "reconcile-v8")


def test_m04_off_target_still_background():
    """M04 類（濃煙燻眼妝配什麼唇色，沒問怎麼畫）：只講眼影的主張照舊降背景。"""
    d = delivered(("濃煙燻妝可以用深灰色眼影加強眼部輪廓。", ["u1"]), ("搭配裸色唇膏讓重點留在眼睛。", ["u2"]))
    out = PP.reconcile(d, "畫了濃煙燻眼妝，唇色要怎麼配比較好？", [])
    assert [c.get("role") for c in out["claims"]] == ["background", "direct"]
