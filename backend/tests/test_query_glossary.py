"""SYNTHETIC terminology tests; these are not retrieval or answer-quality scores."""

import json

import pytest

from app import query_glossary as g


@pytest.mark.parametrize(("query", "expected", "category"), [
    ("原生唇色很深", "naturally pigmented", "原生唇色"),
    ("原生唇色比較深", "naturally pigmented", "原生唇色"),
    ("天生唇色較深", "naturally pigmented", "原生唇色"),
    ("遮蓋力不夠", "coverage", "遮蓋程度"),
    ("淺色蓋不住原本唇色", "coverage", "遮蓋程度"),
    ("嘴唇容易乾", "dryness", "唇部狀態"),
    ("有脫皮", "flaky", "唇部狀態"),
    ("有乾裂", "chapped", "唇部狀態"),
    ("口紅結塊", "cakey", "妝效"),
    ("想要霧面", "matte", "妝效"),
    ("想要亮面", "glossy", "妝效"),
    ("想要薄透", "sheer", "遮蓋程度"),
    ("不透明唇彩", "opaque", "遮蓋程度"),
    ("乳霜質地", "creamy", "質地"),
    ("帶細閃", "shimmery", "妝效"),
    ("金屬光", "metallic", "妝效"),
    ("護唇膏", "lip balm", "產品種類"),
    ("液態唇膏", "liquid lipstick", "產品種類"),
    ("唇蜜", "lip gloss", "產品種類"),
    ("唇油", "lip oil", "產品種類"),
    ("含油配方", "oils", "成分"),
    ("玻尿酸", "hyaluronic acid", "成分"),
    ("唇部妝前乳", "lip primer", "產品種類"),
    ("唇線筆", "lip liner", "產品種類"),
    ("粉底液", "foundation", "產品種類"),
    ("透明蜜粉", "translucent setting powder", "產品種類"),
    ("薄擦一層", "thin layer", "手法"),
    ("等它定型", "let it set", "手法"),
    ("用面紙吸掉多餘", "blot", "手法"),
    ("用面紙輕壓", "press", "手法"),
    ("用海綿輕拍", "stipple", "手法"),
    ("描邊然後填滿", "fill in", "手法"),
    ("邊界暈染", "blend", "手法"),
    ("去角質", "exfoliation", "手法"),
    ("原本是中性底調", "neutral undertone", "底調"),
    ("冷底調", "cool undertone", "底調"),
    ("暖底調", "warm undertone", "底調"),
    ("膚色比較深", "dark skin", "膚色深淺"),
    ("白皙膚色", "fair", "膚色深淺"),
    ("中等膚色", "medium skin", "膚色深淺"),
    ("煙燻眼妝", "smokey eye", "搭配情境"),
    ("上班", "office", "場合"),
    ("乾燥玫瑰色", "dusty rose", "色彩詞"),
    ("珊瑚色", "coral", "色彩詞"),
    ("藕紫色", "mauves", "色彩詞"),
    ("藍調紅", "blue-red", "色彩詞"),
    ("橘調紅", "orange-red", "色彩詞"),
    ("MLBB", "My Lips But Better", "妝效目標"),
    ("明度", "lightness", "色彩屬性"),
    ("彩度", "chroma", "色彩屬性"),
    ("飽和度", "saturation", "色彩屬性"),
])
def test_domain_terms(query, expected, category):
    result = g.analyze(query)
    assert expected in result.english_phrases
    assert any(m.category == category and expected in m.english for m in result.matches)


@pytest.mark.parametrize("query", ["護唇膏", "液態唇膏", "原生唇色很深", "乾燥玫瑰色"])
def test_longest_match_keeps_specific_meaning(query):
    assert len(g.analyze(query).matches) == 1


@pytest.mark.parametrize("query", [
    "中性風", "黃皮", "冷白皮", "不要太亮",
    "絲絨", "柔霧", "水光", "鏡面", "緞光", "珠光", "咬唇妝", "漸層唇妝",
    # v3.3（2026-11-02）：唇釉改為等同液態唇膏，移出待確認清單（見 test_glossary_v33.py）
    "Blushed Rose", "Velvet Underground",
])
def test_uncertain_terms_are_preserved_instead_of_forced_translation(query):
    result = g.analyze(query)
    assert not result.english_phrases
    assert result.native_phrases == (query,)
    assert result.matches[0].needs_review
    assert result.original_query == query


def test_dusty_rose_is_not_dry_lips_and_mlbb_is_not_nude():
    result = g.analyze("MLBB 和乾燥玫瑰色")
    assert set(result.english_phrases) == {"MLBB", "My Lips But Better", "dusty rose"}
    assert not {"dryness", "chapped", "matte", "nude"} & set(result.english_phrases)


def test_product_finish_opacity_and_ingredient_are_independent():
    result = g.analyze("薄透的霧面唇膏，含玻尿酸")
    assert {m.category for m in result.matches} == {"遮蓋程度", "妝效", "產品種類", "成分"}
    assert {"sheer", "matte", "lipstick", "hyaluronic acid"} <= set(result.english_phrases)
    assert "hydrating" not in result.english_phrases


def test_dry_lips_do_not_invent_treatment_or_steps():
    result = g.analyze("嘴唇容易乾，但想擦霧面")
    assert set(result.english_phrases) == {"dryness", "matte"}
    assert not {"lip balm", "lip primer", "exfoliation", "thin layer"} & set(result.english_phrases)


def test_lip_pigmentation_does_not_infer_skin_tone_or_product_color():
    result = g.analyze("原生唇色很深，偏紫")
    assert set(result.english_phrases) == {"naturally pigmented", "pigmented lips"}
    assert all(m.category != "膚色深淺" and m.category != "底調" for m in result.matches)


def test_ambiguous_lip_depth_is_not_forced_into_natural_pigmentation():
    result = g.analyze("我想要唇色深一點")
    assert "pigmented lips" not in result.english_phrases
    assert result.matches[0].key == "lip_depth_ambiguous"
    assert result.matches[0].needs_review


def test_gradient_and_bitten_lip_keep_distinct_review_entries():
    result = g.analyze("咬唇妝和漸層唇妝")
    assert [m.key for m in result.matches] == ["bitten_lip", "gradient"]
    assert not result.english_phrases


def test_negation_is_separate_and_original_is_kept():
    query = "不要霧面或光澤，想要薄透的唇蜜"
    result = g.analyze(query)
    assert result.original_query == query
    assert set(result.negated_phrases) == {"matte", "glossy"}
    assert set(result.english_phrases) == {"sheer", "lip gloss"}


def test_negation_of_finish_does_not_remove_the_product_noun():
    result = g.analyze("我不要霧面唇膏")
    assert result.negated_phrases == ("matte",)
    assert result.english_phrases == ("lipstick",)


def test_negated_undertone_is_not_a_style_preference():
    result = g.analyze("我不是中性底調，我走中性風")
    assert result.negated_phrases == ("neutral undertone",)
    assert not result.english_phrases
    assert result.native_phrases == ("中性風",)


def test_english_codes_hex_and_spelling_are_preserved_without_correction():
    query = "mlbb、HEX #FF7F50、NARS 999、lipstickk"
    result = g.analyze(query)
    assert {"MLBB", "My Lips But Better", "HEX", "#FF7F50", "NARS", "lipstickk"} <= set(result.english_phrases)
    assert result.original_query == query
    assert "lipstick" not in result.english_phrases
    assert all(m.key != "mlbb" for m in g.analyze("xmlbbx").matches)


def test_english_foundation_does_not_translate_research_into_cosmetics():
    result = g.analyze("foundation for the studies")
    assert not any(m.key == "foundation" for m in result.matches)
    assert "粉底" not in json.dumps(result.to_dict(), ensure_ascii=False)


def test_offsets_version_fingerprint_and_dedup_are_stable():
    query = "  護唇膏＋霧面；霧面  "
    result = g.analyze(query)
    assert result == g.analyze(query)
    assert result.original_query == query
    assert result.english_phrases.count("matte") == 1
    assert all(query[m.start:m.end] == m.text for m in result.matches)
    assert result.version == g.GLOSSARY_VERSION
    assert len(result.lexicon_sha256) == 64
    assert json.loads(json.dumps(result.to_dict(), ensure_ascii=False))["original_query"] == query
    assert g.expand(query) == list(result.english_phrases)


def test_empty_or_unrelated_query_does_not_invent_terms():
    assert not g.expand("")
    assert not g.expand("今天天氣如何？")
    with pytest.raises(TypeError):
        g.expand(None)


@pytest.mark.parametrize("query", ["小麥色", "小麥膚色", "小麥色肌膚", "小麥色皮膚"])
def test_wheatish_is_searchable_without_inferred_undertone(query):
    result = g.analyze(query)
    assert set(result.english_phrases) == {"wheatish skin", "wheatish skin tone"}
    assert not result.native_phrases
    assert all(m.category != "底調" for m in result.matches)
    assert not {"tan skin", "dark skin", "warm undertone", "olive skin"} & set(result.english_phrases)


@pytest.mark.parametrize("query", ["豆沙色", "豆沙", "玫瑰豆沙", "玫瑰豆沙色"])
def test_bean_paste_has_brand_alias_and_separate_related_descriptor(query):
    result = g.analyze(query)
    assert result.english_phrases == ("rosewood",)
    assert result.related_phrases == ("dusty rose",)
    assert not result.native_phrases
    assert g.expand(query) == ["rosewood"]
    assert g.expand(query, include_related=True) == ["rosewood", "dusty rose"]


def test_milk_tea_alias_does_not_replace_every_nude_color():
    result = g.analyze("奶茶色")
    assert result.english_phrases == ("milk tea",)
    assert result.related_phrases == ("nude beige",)
    assert not result.native_phrases
    assert "milk tea" not in g.expand("裸色")
    assert "nude" not in g.expand("奶茶色")


def test_related_terms_respect_negation():
    result = g.analyze("不要豆沙色或奶茶色")
    assert not result.english_phrases
    assert not result.related_phrases
    assert set(result.negated_phrases) == {"rosewood", "milk tea"}
    assert set(result.negated_related_phrases) == {"dusty rose", "nude beige"}
    assert not g.expand(result.original_query, include_related=True)


def test_dusty_rose_and_bean_paste_remain_distinct_terms():
    result = g.analyze("豆沙色和乾燥玫瑰色")
    assert [m.key for m in result.matches] == ["bean_paste", "dusty_rose"]
    assert g.expand(result.original_query, include_related=True) == ["rosewood", "dusty rose"]


def test_web_evidence_is_registered_without_becoming_answer_evidence():
    for key in ("wheat_skin", "bean_paste", "milk_tea"):
        term = next(t for t in g.TERMS if t.key == key)
        assert term.sources and all(source in g.WEB_SOURCES for source in term.sources)
        assert all(g.WEB_SOURCES[source]["url"].startswith("https://") for source in term.sources)
    assert g.analyze("小麥色想擦豆沙色").original_query == "小麥色想擦豆沙色"


@pytest.mark.parametrize("query,key,expected", [
    ("我對羊毛脂過敏", "lanolin", "lanolin"),
    ("想了解羊毛脂醇", "lanolin_alcohol", "lanolin alcohol"),
    ("香精會有什麼問題", "fragrance", "parfum"),
    ("檸檬烯", "limonene", "limonene"),
    ("芳樟醇", "linalool", "linalool"),
    ("檸檬醛", "citral", "citral"),
    ("桂皮醛", "cinnamal", "cinnamal"),
    ("丁香酚", "eugenol", "eugenol"),
    ("香葉醇", "geraniol", "geraniol"),
    ("苯甲醇", "benzyl_alcohol", "benzyl alcohol"),
    ("薄荷油", "peppermint_oil", "Mentha Piperita Oil"),
    ("MIT", "mi", "methylisothiazolinone"),
    ("CMIT", "mci", "methylchloroisothiazolinone"),
    ("甲基異噻唑啉酮", "mi", "methylisothiazolinone"),
    ("DMDM乙內醯脲", "dmdm_hydantoin", "DMDM hydantoin"),
    ("雙咪唑烷基脲", "diazolidinyl_urea", "diazolidinyl urea"),
    ("咪唑烷基脲", "imidazolidinyl_urea", "imidazolidinyl urea"),
    ("季銨鹽15", "quaternium_15", "quaternium-15"),
    ("溴硝丙二醇", "bronopol", "bronopol"),
    ("羥苯酯類", "parabens", "parabens"),
    ("蜂膠", "propolis", "propolis"),
    ("cera alba", "beeswax", "beeswax"),
    ("蓖麻油", "castor_oil", "Ricinus Communis Seed Oil"),
    ("氫化蓖麻油", "hydrogenated_castor_oil", "hydrogenated castor oil"),
    ("蓖麻油酸", "ricinoleic_acid", "ricinoleic acid"),
    ("丙二醇", "propylene_glycol", "propylene glycol"),
    ("松香", "colophony", "rosin"),
    ("蟲膠", "shellac", "shellac"),
    ("胭脂蟲紅", "carmine", "CI 75470"),
    ("胭脂蟲萃取物", "cochineal_extract", "cochineal extract"),
    ("氧苯酮", "benzophenone_3", "benzophenone-3"),
    ("沒食子酸丙酯", "propyl_gallate", "propyl gallate"),
])
def test_ingredient_names_are_available_for_search_without_diagnosis(query, key, expected):
    result = g.analyze(query)
    assert expected in result.english_phrases
    assert [m.key for m in result.matches] == [key]
    assert result.original_query == query
    assert "allergy_status" not in result.to_dict()


def test_inci_phrases_are_preserved_as_one_registered_match():
    result = g.analyze("CI  75470、RiCiNuS Communis (Castor) Seed Oil、MENTHA PIPERITA OIL")
    assert [m.key for m in result.matches] == ["carmine", "castor_oil", "peppermint_oil"]
    assert all(result.original_query[m.start:m.end] == m.text for m in result.matches)


def test_parent_ingredient_and_derivatives_are_not_collapsed():
    query = "羊毛脂醇、蓖麻油酸、氫化蓖麻油、雙咪唑烷基脲、甲醛釋放型防腐劑"
    result = g.analyze(query)
    assert [m.key for m in result.matches] == ["lanolin_alcohol", "ricinoleic_acid", "hydrogenated_castor_oil",
                                              "diazolidinyl_urea", "formaldehyde_releasers"]
    assert not {"lanolin", "castor oil", "imidazolidinyl urea", "formaldehyde"} & set(result.english_phrases)


def test_propolis_and_beeswax_do_not_expand_into_each_other():
    assert g.expand("蜂膠") == ["propolis"]
    assert g.expand("蜂蠟") == ["beeswax"]
    assert g.expand("蜂蜜") == []


def test_individual_isothiazolinones_and_mix_stay_distinct():
    assert [m.key for m in g.analyze("MI、MCI、MCI/MI、Kathon CG").matches] == ["mi", "mci", "mci_mi_mix", "mci_mi_mix"]
    assert g.expand("MI") == ["methylisothiazolinone"]
    assert g.expand("MCI") == ["methylchloroisothiazolinone"]
    assert [m.key for m in g.analyze("MITCHELL LIMITED").matches] == ["literal", "literal"]


@pytest.mark.parametrize("query", ["不要羊毛脂或蜂膠", "avoid lanolin or propolis", "free of lanolin and propolis"])
def test_avoided_ingredients_are_separate_from_positive_search_phrases(query):
    result = g.analyze(query)
    assert not result.english_phrases
    assert {"lanolin", "propolis"} <= set(result.negated_phrases)
    assert result.original_query == query


def test_free_from_label_language_is_not_a_safety_verdict():
    result = g.analyze("fragrance-free lipstick and lanolin free lip balm")
    assert {"fragrance", "parfum", "perfume", "lanolin"} <= set(result.negated_phrases)
    assert "lipstick" in result.english_phrases
    assert "safe" not in result.english_phrases


@pytest.mark.parametrize("query", ["我對羊毛脂過敏", "我沒有羊毛脂過敏", "羊毛脂會過敏嗎", "不確定是否對羊毛脂過敏"])
def test_ingredient_mentions_never_set_or_clear_allergy_state(query):
    result = g.analyze(query)
    assert any(m.key == "lanolin" for m in result.matches)
    assert result.original_query == query
    assert not {"allergy", "allergy_status", "has_known", "none_known", "avoid_items"} & result.to_dict().keys()


@pytest.mark.parametrize("query", ["胭脂紅", "1,3-丙二醇", "PEG-40 氫化蓖麻油"])
def test_ambiguous_or_different_ingredients_keep_their_full_name(query):
    result = g.analyze(query)
    assert not result.english_phrases
    assert result.native_phrases == (query,)
    assert result.matches[0].needs_review


def test_red_pigment_is_not_a_red_lipstick_preference():
    result = g.analyze("我對胭脂蟲紅過敏，想要奶茶色")
    assert [m.key for m in result.matches] == ["carmine", "milk_tea"]
    assert result.matches[0].category == "成分／色料"
    assert "red" not in result.english_phrases


def test_fragrance_and_preservative_classes_do_not_expand_to_all_members():
    assert not {"limonene", "linalool", "citral"} & set(g.expand("香精"))
    assert g.expand("防腐劑") == ["preservatives"]
    assert "methylisothiazolinone" not in g.expand("羥苯酯類")


def test_all_ingredient_entries_have_traceable_name_sources():
    assert len(g.ALLERGEN_TERMS) >= 30
    for term in g.ALLERGEN_TERMS:
        assert term.sources
        assert all(source in g.WEB_SOURCES for source in term.sources)


# v3.1：過年／喜氣（N05）。英文只用來找中華文化紅色象徵段落；跨年、好運這類說法不展開。
@pytest.mark.parametrize("query", ["過年回家想擦有喜氣的顏色", "拜年要擦什麼顏色比較喜氣", "新年想要看起來喜慶一點",
                                   "春節聚餐擦什麼唇色", "農曆新年想擦紅色"])
def test_new_year_and_festive_terms_expand_to_source_words(query):
    result = g.analyze(query)
    assert {m.category for m in result.matches if m.key == "lunar_new_year"} == {"文化象徵"}
    assert result.english_phrases[:2] == ("good luck", "prosperity")


@pytest.mark.parametrize("query", ["跨年派對要擦什麼", "我想要看起來好運一點", "想要一個繁榮的感覺"])
def test_countdown_and_plain_luck_are_not_new_year(query):
    assert "good luck" not in g.expand(query) and "prosperity" not in g.expand(query)


def test_negated_new_year_is_not_searched():
    result = g.analyze("不是過年用，平常擦的")
    assert result.english_phrases == () and result.negated_phrases == ("good luck", "prosperity")



def test_v35_recall_terms_only_for_their_phrases():
    """v3.5（2026-11-07）：N02「不會太搶戲」→ overpowering／less is more；W09「看起來比較白／紅潤」→ perceived facial skin tone／colour assimilation。"""
    n02 = g.analyze("我平常穿得很簡約、偏中性風，想找不會太搶戲的唇色。")
    assert {"overpowering", "less is more"} <= {e for m in n02.matches for e in m.english}
    w09 = g.analyze("擦不同顏色的口紅，會讓臉看起來比較白或比較紅潤嗎？")
    assert {"perceived facial skin tone", "colour assimilation"} <= {e for m in w09.matches for e in m.english}
    other = g.analyze("我是冷底，想擦紅色，要選哪一種紅比較適合？")
    assert not {"overpowering", "perceived facial skin tone"} & {e for m in other.matches for e in m.english}
