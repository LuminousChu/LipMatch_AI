"""reconcile-v4 測試：限制句說「證據沒有 X」，X 卻是交付主張合起來已在講、或送入片段就有（2026-10-27）。

主張與限制句文字取自第十七批本機 E2B 保存輸出（accept_e2b_rulev10_20261026；gaus_v1 判讀列為矛盾或應保留）；
來源片段一律是為測試寫的 SYNTHETIC 短句，不是來源原文。不呼叫任何模型。
"""
from __future__ import annotations

from app import postprocess as PP


def _delivered(texts, limitations):
    return {"status": "delivered", "claims": [{"claim_no": i, "text": t, "unit_ids": ["u"], "role": "unjudged",
                                               "source_conditions": []} for i, t in enumerate(texts, 1)],
            "limitations": list(limitations), "removed": [], "flags": [], "system_notes": [],
            "clarifying_question": None, "display_source_conditions": False}


def _rules(r):
    return {x["limitation"]: x["rule"] for x in r["removed"] if x.get("limitation")}


def test_w03_limitation_denying_the_comparison_just_given_is_removed():
    """W03：正文已比較豆沙與奶茶的基底，限制卻說「未說明色調機制上的差異」→ 拿掉（主張聯集）。"""
    claims = ["奶茶色本質上是棕色系中加入白色或米色色素，降低了飽和度與明度。",
              "經典豆沙色是在紅色或粉色中融入灰色調，保留了更多的血色感。"]
    lim = "證據未說明豆沙色與奶茶色在色調機制上的具體差異。"
    r = PP.reconcile(_delivered(claims, [lim]), "豆沙色跟奶茶色差在哪裡？")
    assert r["limitations"] == [] and _rules(r)[lim] == "limitation_contradicts_claims_union"
    assert r["reconcile_version"] in ("reconcile-v5", "reconcile-v6", "reconcile-v7", "reconcile-v8") and not r["system_notes"]   # 正文已講，不加註記


def test_m04_generic_and_occasion_limitations_removed():
    """M04：正文給了夜間與棕煙燻配裸色；「沒有具體建議」「沒說特定場合」都和正文矛盾。"""
    claims = ["如果晚上或參加晚間活動，搭配煙燻眼妝時，選擇裸色霧面唇膏可以營造出更戲劇化但仍平衡的效果。",
              "如果搭配美麗的棕色煙燻眼妝，可以選擇裸色唇膏來搭配，以使整體造型更協調。"]
    lims = ["沒有提供關於如何選擇特定唇色來平衡濃烈煙燻眼妝的具體建議。", "證據中沒有說明在特定場合下應如何搭配唇色。"]
    r = PP.reconcile(_delivered(claims, lims), "眼妝想畫比較濃的煙燻，唇色要怎麼配才不會太重？")
    assert r["limitations"] == [] and set(_rules(r)) == set(lims)


def test_p01_paraphrased_undertone_and_occasion_counted_as_covered():
    """P01：限制說「暖底」「上班場合」，主張寫「膚色偏暖」「辦公室」——換了說法也算已講。"""
    claims = ["如果膚色偏暖，選擇含有橘色和紅色的唇膏，這些顏色能為暖調膚色增添火熱的妝感。",
              "經典豆沙色或偏暖的杏仁奶茶色最為安全，適合辦公室環境，能展現專業且親和的形象。"]
    lims = ["證據中沒有提供針對暖底膚色和上班場合的特定唇色建議。", "證據中沒有說明如何根據暖底膚色選擇適合的唇色。"]
    r = PP.reconcile(_delivered(claims, lims), "我是暖底、唇色偏淡，想找上班也能擦的唇色。")
    assert r["limitations"] == []


def test_m02_step_limitation_needs_a_procedural_claim():
    """M02：限制問「打底的具體步驟」，正文有打底做法 → 拿掉；正文只有選色、沒有做法 → 保留。"""
    lim = "證據未說明如何針對深唇色進行打底的具體步驟。"
    method = ["建議先用少量粉底或遮瑕膏輕拍唇部打底，以中和原有唇色，再選擇顯色度較高的款式。"]
    assert PP.reconcile(_delivered(method, [lim]), "天生唇色深，淺色蓋不住")["limitations"] == []
    choice_only = ["深唇色可以改選帶有紅調的豆沙色。"]
    assert PP.reconcile(_delivered(choice_only, [lim]), "天生唇色深，淺色蓋不住")["limitations"] == [lim]


def test_true_limitations_are_kept():
    """應保留：使用者資訊未知（N02）、主張沒講到的主題（M01 小麥色、P04 粉腮紅）、只用沒說怎麼選（v1 規則）。"""
    n02 = PP.reconcile(_delivered(["經典豆沙色或偏暖的杏仁奶茶色不會過於張揚。"], ["無法根據使用者膚色或底調提供具體的選色建議。"]), "想找不搶戲的唇色")
    assert len(n02["limitations"]) == 1
    m01 = PP.reconcile(_delivered(["如果皮膚有暖調底色，建議選擇珊瑚色或磚紅。", "如果皮膚有冷調底色，建議選擇灰玫瑰。"],
                                  ["證據沒有說明小麥色皮膚的具體底調。", "證據沒有提供針對小麥色皮膚的特定唇膏推薦。"]), "我是小麥色皮膚")
    assert len(m01["limitations"]) == 2
    p04 = PP.reconcile(_delivered(["對於冷底膚色，柔和的粉紅色、灰紫都適用。"], ["證據未說明如何搭配已有的粉色腮紅。"]),
                       "哪個比較好搭我已經有的粉色腮紅？", ["SYNTHETIC: a buildable pink blush flatters cool skin."])
    assert p04["limitations"] == ["證據未說明如何搭配已有的粉色腮紅。"]   # 片段有 pink blush，但沒講搭配
    primer = PP.reconcile(_delivered(["為了持久，可以塗一層保濕的唇妝前乳。"], ["證據未說明如何選擇適合的唇妝前乳。"]), "霧面不乾？")
    assert len(primer["limitations"]) == 1


def test_limitation_contradicted_by_sent_chinese_evidence_is_removed_with_note():
    """P05：正文沒用到，但送入片段在講「中和誇張」→ 拿掉限制、加旗標與系統註記；片段只沾到一點邊不算。"""
    lim = "證據未說明如何達到「不誇張」的效果。"
    claims = ["使用櫻桃紅色唇膏填充嘴唇中央，再輕輕融合。"]
    ev = ["SYNTHETIC：兩種紅色融合，加上一點紅棕色調，可以中和誇張的效果。"]
    r = PP.reconcile(_delivered(claims, [lim]), "想試不一樣的紅唇但不要太誇張", ev)
    assert r["limitations"] == [] and _rules(r)[lim] == "limitation_topic_in_evidence"
    assert any(f["flag"] == "evidence_topic_unused" for f in r["flags"]) and "沒有用到" in r["system_notes"][0]
    off = PP.reconcile(_delivered(claims, [lim]), "想試不一樣的紅唇但不要太誇張", ["SYNTHETIC：紅唇是經典妝容。"])
    assert off["limitations"] == [lim]


def test_glossary_english_alias_and_comparison_limitations():
    """W04：「乾燥玫瑰色」經術語表對到英文片段的 dusty rose → 拿掉；「X 和 Y 之間的差異」不因片段有兩個詞就拿掉。"""
    ev = ["SYNTHETIC: Blue-pinks like fuchsia and dusty rose suit cool skin. MLBB means my lips but better."]
    claims = ["MLBB 色彩通常指的是能增強自然唇色的唇彩。"]
    lim_info = "證據未提供關於乾燥玫瑰色的詳細資訊。"
    lim_cmp = "證據未說明 MLBB 和乾燥玫瑰色之間的具體差異。"
    r = PP.reconcile(_delivered(claims, [lim_info, lim_cmp]), "MLBB 和乾燥玫瑰色差在哪？", ev)
    assert r["limitations"] == [lim_cmp]
    assert "dusty rose" in next(x for x in r["removed"] if x["limitation"] == lim_info)["reason"]


def test_helpers_parse_topics():
    """主題拆解：概括限制主題為空；使用者資訊不拆；非「沒有＋動詞」句型不拆。"""
    assert PP._limit_topic("沒有提供具體建議。").empty
    assert PP._limit_topic("無法根據使用者膚色提供建議。") is None
    assert PP._limit_topic("研究未建立標準化的唇妝顏色系統。") is None     # 已知漏網（只和英文片段矛盾）
    tp = PP._limit_topic("證據中沒有說明在特定場合下應如何搭配唇色。")
    assert len(tp.need) == 1 and tp.grams == {"搭配"} and not tp.specific
    assert PP._limit_topic("證據中未提供針對冷底調搭配橘色系的產品推薦。").specific == ("產品",)


def test_heldout_t4_true_limitations_kept():
    """T4 12B 保存輸出（accept_t4_20261027 詞表關）裡 v4 初版誤刪、其實正確的限制：要保留。"""
    m04 = PP.reconcile(_delivered(["如果是夜間活動且搭配煙燻眼妝，選擇裸色霧面唇膏可以創造出平衡且有戲劇性的效果。"],
                                  ["證據未提供針對白天煙燻眼妝搭配唇色的具體建議。"]), "濃煙燻配什麼唇色？")
    assert len(m04["limitations"]) == 1                                  # 白天 ≠ 夜間
    p02 = PP.reconcile(_delivered(["如果是冷底調，選擇偏向粉紅色或褐色調的冷色系橘色，會與您的膚色完美搭配。"],
                                  ["證據中未提供針對冷底調搭配橘色系的產品推薦。"]), "冷底想擦橘色")
    assert len(p02["limitations"]) == 1                                  # 系統不給產品，限制是真的
    p04 = PP.reconcile(_delivered(["對於冷底調的膚色，藍調紅是適合的選擇。", "如果是冷底調，鮮明紅也是適合的選擇。"],
                                  ["證據中未提供關於粉色腮紅與藍調紅或鮮明紅唇膏的對比建議。"]), "哪個好搭粉色腮紅？")
    assert len(p04["limitations"]) == 1                                  # 講了兩種紅，沒講粉腮紅
    m02 = PP.reconcile(_delivered(["建議先用少量粉底或遮瑕膏輕拍唇部打底，以中和原本的唇色。"],
                                  ["證據未提及如何針對特定唇色進行對比測試的具體步驟。"]), "深唇蓋不住",
                       ["SYNTHETIC：我把幾支色號在手臂和嘴唇上做了對比測試。"])
    assert len(m02["limitations"]) == 1                                  # 片段提到測試不等於有步驟


# ---------- 深唇主題補偏紫、偏暗（M02，2026-10-27） ----------

M02_Q = "我天生唇色比較深、偏紫，擦淺色口紅好像蓋不住原本的顏色。"
M02_CLAIMS = ["建議先用少量粉底或遮瑕膏輕拍唇部打底，以中和原本的唇色。",
              "在打底後，可以選擇顯色度較高的款式，或改選帶有紅調的豆沙色以確保顯色純正。"]


def test_m02_example_in_parentheses_is_lip_depth_and_removed():
    """E2B M02：模型把唇色寫成「特定膚色（如偏紫）」，括號例子才是在講的東西；正文已給深唇的顏色建議 → 拿掉。"""
    lim = "證據未提供針對特定膚色（如偏紫）的顏色建議。"
    r = PP.reconcile(_delivered(M02_CLAIMS, [lim]), M02_Q)
    assert r["limitations"] == [] and _rules(r)[lim] == "limitation_contradicts_claims_union"


def test_m02_named_purple_needs_purple_in_claims():
    """T4 詞表開 M02：限制直接點名「偏紫」，主張只講中和原本唇色、沒講紫 → 保留；產品推薦一律保留。"""
    lims = ["證據未提供針對「偏紫」唇色的特定中和顏色建議。", "證據未提供針對深色或偏紫唇色具體的產品推薦。"]
    r = PP.reconcile(_delivered(M02_CLAIMS, lims), M02_Q)
    assert r["limitations"] == lims


def test_dark_lip_phrasings_count_as_lip_depth_topic():
    """「唇色偏暗」「偏紫的唇」算深唇主題；主張講到同一種（暗／紫）才拿掉。單說偏紫色口紅是唇彩顏色，不算深唇。"""
    claims = ["唇色偏暗或偏紫的唇，可以先用遮瑕打底，再選色上妝。"]
    for lim in ("證據沒有說明唇色偏暗時的建議。", "證據沒有提到偏紫的唇要怎麼選色。"):
        assert PP.limitation_topic_covered(lim, claims), lim
    assert PP.limitation_topic_covered("證據沒有唇色偏暗時的建議。", ["唇色比較深時可先打底。"]) is None
    tp = PP._limit_topic("證據沒有提供偏紫色口紅的建議。")
    assert tp is not None and not tp.need   # 不是深唇主題：剩下的「偏紫」要主張有原詞


def test_words_containing_stop_chars_are_kept_as_topic():
    """「中和」「柔和」含單字停用字（中、和）：修前整個被切掉、主題變空，有任何主張就被當成矛盾拿掉。
    現在保留成主題詞：主張沒講中和就保留，講了才拿掉；一般連接詞「和」照舊切開。"""
    lim = "證據沒有說明中和的做法。"
    assert PP.limitation_topic_covered(lim, ["可以改選帶有紅調的豆沙色。"]) is None
    assert PP.limitation_topic_covered(lim, ["先用遮瑕打底，中和原本的唇色。"])
    assert [sorted(s) for s in PP._limit_topic("證據沒有說明柔和妝感的做法。").segs] == [["和妝", "妝感", "柔和"]]
    assert [sorted(s) for s in PP._limit_topic("證據沒有說明唇膏和唇蜜的差異。").segs] == [["唇蜜"]]


# 第五十一批：主張、限制取自 T4 合併版本詞表開保存輸出（accept_t4_v2_20261028）；片段句是 SYNTHETIC 改寫
N01_Q = "我嘴唇容易乾，但又想擦霧面，有什麼辦法讓它不要看起來乾乾的？"
N01_CLAIMS = ["如果是嘴唇乾燥或起皮，在上色前先進行去角質。",
              "在塗抹前使用含有神經醯胺或荷荷巴油的護唇膏進行短暫濕敷，並擦掉多餘油脂。",
              "塗抹一層薄薄的護唇膏，用面紙輕壓吸掉多餘，再上霧面唇膏。",
              "選擇含有保濕因子或含油、透明質酸的輕盈霧面配方。"]


def test_n01_exfoliation_steps_kept_when_claim_only_names_the_step():
    """T4 詞表開 N01：主張只說「先去角質」，沒說怎麼去 →「沒說明去角質的具體步驟」保留；主張有手段（磨砂、毛巾）才拿掉。"""
    lim = "證據未說明去角質的具體操作步驟。"
    assert lim in PP.reconcile(_delivered(N01_CLAIMS, [lim]), N01_Q)["limitations"]
    how = ["用濕毛巾輕輕畫圈去角質，再擦護唇膏。"]
    assert PP.reconcile(_delivered(how, [lim]), N01_Q)["limitations"] == []
    # 時間子句「在去角質後」不算講去角質本身
    assert PP.limitation_topic_covered(lim, ["在去角質後用指腹塗一層護唇膏。"]) is None


def test_n01_moisturizer_kind_trimmed_and_brand_kept():
    """T4 詞表開 N01：「沒有品牌或保濕因子種類」，主張已寫神經醯胺、透明質酸 → 只留「沒有品牌」；
    只說「含保濕因子」不算講了種類（舊 N01「保濕因子名稱」照舊保留）。"""
    lim = "證據未提供具體的護唇膏品牌或保濕因子種類。"
    r = PP.reconcile(_delivered(N01_CLAIMS, [lim]), N01_Q)
    assert r["limitations"] == ["證據未提供具體的護唇膏品牌。"]
    rm = next(x for x in r["removed"] if x.get("limitation") == lim)
    assert rm["rule"] == "limitation_part_contradicts_claims" and rm["parts"] == ["保濕因子種類"]
    vague = ["選擇含有保濕因子的現代霧面配方。"]
    assert PP.reconcile(_delivered(vague, [lim]), N01_Q)["limitations"] == [lim]
    assert PP.reconcile(_delivered(vague, ["證據未提供保濕因子名稱。"]), N01_Q)["limitations"] == ["證據未提供保濕因子名稱。"]


def test_trim_leaves_generic_or_fully_covered_limitations_alone():
    """「或」的某一段只剩概括詞、或兩段都講到／都沒講到 → 不切。"""
    assert PP.limitation_trim_covered("證據未提供具體建議或保濕因子種類。", N01_CLAIMS) is None
    assert PP.limitation_trim_covered("證據未提供護唇膏品牌或色號。", N01_CLAIMS) is None


def test_m02_brand_comparison_not_removed_by_term_in_evidence():
    """T4 詞表開 M02：限制問「深唇色與品牌產品的對比」，片段只有「深唇色」三個字 → 保留；W04 型（無品牌）照舊拿掉。"""
    lim = "證據未提供針對「深唇色」與特定品牌產品的對比建議。"
    ev = ["SYNTHETIC：深唇色會改變口紅的顯色，建議先打底再選顯色度高的款式。"]
    assert PP.reconcile(_delivered(M02_CLAIMS, [lim]), M02_Q, ev)["limitations"] == [lim]
    w04 = "證據中未提供乾燥玫瑰色（dusty rose）的具體色彩特徵或與 MLBB 的對比數據。"
    r = PP.reconcile(_delivered(["MLBB 唇彩通常具有中等亮度與低飽和度。"], [w04]), "MLBB 和乾燥玫瑰色差在哪？",
                     ["SYNTHETIC: Blue-pinks, like dusty rose, suit cool skin. MLBB shades are muted."])
    assert r["limitations"] == []


def test_regressions_from_replay_are_kept():
    """重放抓到的誤傷（第五十一批改法前）：比較／區分型限制的「或」不切（舊 T4 P04_t2、W01）；
    舊 T4 N01 神經醯胺只是護唇膏成分、霧面配方「含保濕因子」沒名稱 →「沒有保濕因子名稱」保留。"""
    p04 = "證據中未提供關於粉色腮紅與藍調紅或鮮明紅唇膏的對比建議。"
    assert PP.limitation_trim_covered(p04, ["冷底適合藍調紅。", "鮮明紅唇膏可以搭配淡妝。"]) is None
    w01 = "證據中未提供具體的關鍵字建議以區分非粉紅或非橙紅的紅色。"
    assert PP.limitation_trim_covered(w01, ["正紅不偏粉也不偏橘。"]) is None
    old = N01_CLAIMS[:3] + ["選擇含有保濕因子的現代霧面配方，以維持唇部舒適度。"]
    lim = "證據未提供具體的保濕因子名稱。"
    assert PP.reconcile(_delivered(old, [lim]), N01_Q)["limitations"] == [lim]


def test_trim_skips_limitation_with_second_clause():
    """第五十三批（T4 v4 詞表關 P03）：限制後面接「，僅提供 A 或 B」時不切，否則會變成「…僅提供裸色。」的殘句。"""
    lim = "證據中沒有關於「不要太亮」的具體色調建議，僅提供裸色或柔和色調的搭配方式。"
    claims = ["可以選擇柔和色調的唇彩，搭配裸色眼妝。", "裸色系唇膏適合日常。"]
    assert PP.limitation_trim_covered(lim, claims) is None
