"""rule-v15／need-rel-v4（2026-11-01）解析器修正：四項分開測。

1. 「分別」等含「別」的詞不當否定（第 3 類假衝突，N07）；單字「別」仍是否定。
2. 「搭配」不再單獨等於推薦。
3. 只被提到的色系／妝感不當想要；同一子句明說在找（推薦、想找、幫我挑…）才算。
4. 膚色口語（「皮膚很白」「膚色比較深」）記成 skin_tone，不推定底調；排序對得到英文比較級（S1）。

句子是 40 題原句或標 SYNTHETIC 的開發句。
"""
from __future__ import annotations

import re

import pytest

from app import need_relevance as NR
from app import preferences as pref

P = pref.RuleParser()
N07 = "我是冷底，過年想擦有喜氣的紅唇。請分別說明紅色的文化依據，以及冷底挑紅色的方向。"
W06 = "MLBB 和霧面是在說同一種唇膏顏色嗎？如果我想修飾自然唇色、又不要亮面，這兩個詞應分別怎麼理解？"
M07 = "我皮膚很白，擦很濃烈的口紅會不會反而顯得蒼白？如果想擦紅色或粉色要怎麼挑？"
M08 = "我膚色比較深，可是很喜歡淺色口紅，有什麼擦法比較好看？"


def _fields(text: str) -> set[tuple[str, str | None]]:
    return {(c["field"], c.get("key") or c.get("value")) for c in P.parse(text).changes}


# ---------------------------------------------------------------------------
# 1. 「分別」不是否定
# ---------------------------------------------------------------------------
def test_n07_no_false_conflict_and_red_not_disliked():
    p = P.parse(N07)
    assert p.conflicts == [] and ("disliked_colors", "red") not in _fields(N07)
    assert p.intent == "knowledge"          # 「請分別說明」是指令句（K1 規則補「分別」）


@pytest.mark.parametrize("text", ["請分別說明紅色和粉色", "這兩個顏色差別在哪", "特別想擦紅色"])   # SYNTHETIC
def test_bie_compounds_are_not_negation(text):
    assert not any(f == "disliked_colors" for f, _ in _fields(text))


def test_single_bie_is_still_negation():
    assert ("disliked_colors", "red") in _fields("別擦紅色")                          # SYNTHETIC
    p = P.parse("別太紅")                                                             # SYNTHETIC
    assert any(a["kind"] == "degree" for a in p.ambiguities)


# ---------------------------------------------------------------------------
# 2. 「搭配」不是推薦詞
# ---------------------------------------------------------------------------
def test_dapei_alone_is_not_recommend():
    assert not pref._RE_RECOMMEND.search("這件衣服要怎麼搭配")                       # SYNTHETIC
    assert P.parse("今天穿黑色洋裝，搭配什麼唇色好？").intent == "recommend"          # 「什麼唇色」仍是推薦詞（SYNTHETIC）


# ---------------------------------------------------------------------------
# 3. 只被提到的詞不當想要
# ---------------------------------------------------------------------------
def test_w06_mentioned_matte_is_not_liked():
    f = _fields(W06)
    assert ("liked_finishes", "matte") not in f and ("disliked_finishes", "gloss") in f


@pytest.mark.parametrize("text, expected", [
    ("推薦霧面的唇膏", ("liked_finishes", "matte")),                                   # SYNTHETIC
    ("幫我挑豆沙色的口紅", ("liked_colors", "bean_paste")),                            # SYNTHETIC
])
def test_seek_in_same_clause_still_counts_as_wanted(text, expected):
    assert expected in _fields(text)


def test_mention_next_to_dislike_is_not_a_conflict():
    p = P.parse("霧面，不要霧面")                                                       # SYNTHETIC
    assert p.conflicts == [] and _fields("霧面，不要霧面") == {("disliked_finishes", "matte")}
    assert P.parse("要霧面，不要霧面").conflicts                                         # 明說要與不要：仍是衝突


# ---------------------------------------------------------------------------
# 4. 膚色口語（S1）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text, tone", [(M07, "膚色偏白"), (M08, "膚色偏深"), ("我膚色有點黃", "膚色偏黃")])
def test_colloquial_skin_tone_recorded_without_undertone(text, tone):
    p = P.parse(text)
    skin = [c for c in p.changes if c["field"] == "skin_tone"]
    assert [c["value"] for c in skin] == [tone] and skin[0]["raw"].startswith(("我皮膚", "我膚色"))
    assert not any(c["field"] == "undertone" for c in p.changes)


@pytest.mark.parametrize("text", ["我皮膚很乾", "白皮膚的人適合什麼唇色？"])            # SYNTHETIC
def test_non_tone_or_general_question_not_recorded(text):
    assert not any(c["field"] == "skin_tone" for c in P.parse(text).changes)


@pytest.mark.parametrize("value, raw, words", [
    ("膚色偏深", "我膚色比較深", {"deep", "deeper", "dark", "darker"}),
    ("膚色偏白", "我皮膚很白", {"fair", "fairer", "light", "lighter"}),
    ("膚色偏深", "我膚色偏深", {"deep", "deeper"}),                                   # 詞表值本身（N11）
])
def test_skin_terms_for_ranking(value, raw, words):
    terms = NR._user_skin_terms([{"field": "skin_tone", "value": value, "raw": raw}])
    assert words <= set(terms)
    pat = re.compile(r"\b(?:" + "|".join(terms) + r")\b", re.I)
    assert pat.search("If you have a deeper skin tone") or pat.search("fairer skin")


def test_versions():
    assert pref.PARSER_VERSION == "rule-v15" and NR.NEED_REL_VERSION == "need-rel-v4"
