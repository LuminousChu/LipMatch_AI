"""2026-10-27 問答流程三項修正（T4 兩輪 GAUS 判讀找到的工程問題）。片段都是 SYNTHETIC。

整支測試在檢查什麼：
1. need-rel-v3：命中「嘴唇」以外的部位（腮紅、臉頰、眼部）會往前提，但排在命中條件／操作／用途的後面；
   使用者自述膚色時，原文條件寫到對應英文膚色詞的片段往前提；膚色不符不降權；沒有自述膚色不受影響。
2. C-pp-v3：模型標 insufficient 但附了主張 → 通過檢查的主張交付（delivered_partial＋系統註記）；
   主張都沒通過 → source_insufficient；沒附主張 → 照舊 source_insufficient。
3. ask-once-v1：系統這一輪已有補問時，問答不交付模型自己的補問（記在日誌）；沒有系統補問時保留。
"""
from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

from app import need_relevance as NR
from app import postprocess as PP
from app.retrieval_policy import Candidate

from tests.test_postprocess import unit
from tests.test_qa import PUBLIC_KEY, FakeGenerator, _claims, _mock_modules
from tests.test_qa import conn, corpus  # noqa: F401  （test_qa 的 fixture：測試庫連線與 SYNTHETIC 語料）


def cand(uid, rank, scope=None, role=None):
    return Candidate(unit_id=uid, text=f"SYNTHETIC {uid}", pool_rank=rank, tokens=10,
                     meta={"support_scope": scope, "content_role": role})


def ids(cs):
    return [c.unit_id for c in cs]


COOL = [{"field": "undertone", "value": "cool", "raw": "我是冷底"}]


# ---------- 1. need-rel-v3 ----------

def test_non_lip_part_promotes_but_after_condition_matches():
    """P04：問腮紅搭配時，只標「部位：臉頰、腮紅」的同色系唇頰片段要能進前段；只命中「嘴唇」仍不提前。"""
    cs = [cand("lip-generic", 1, "部位：嘴唇"),
          cand("cool-red", 2, "條件：冷底；色名：紅色"),
          cand("cool-pink", 3, "條件：冷底；色名：粉色"),
          cand("same-tone-cheek", 24, "部位：臉頰、腮紅"),
          cand("blush-cool", 25, "部位：臉頰、腮紅；條件：冷底")]
    out, log = NR.order_for_pack("哪個比較好搭我已經有的粉色腮紅？", COOL, cs)
    # blush-cool：冷底 2＋部位 2；same-tone-cheek：部位 2；冷底兩段各 2（同分照向量名次）；只有嘴唇的最後
    assert ids(out) == ["blush-cool", "cool-red", "cool-pink", "same-tone-cheek", "lip-generic"]
    row = {r["unit_id"]: r for r in log["order"]}
    assert row["same-tone-cheek"]["part_score"] == 2 and row["same-tone-cheek"]["strong_score"] == 0
    assert row["lip-generic"]["part_score"] == 0
    assert NR.NEED_REL_VERSION == "need-rel-v4"


def test_condition_match_outranks_single_part_match():
    cs = [cand("eye-only", 1, "部位：眼部"), cand("eye-heavy", 2, "部位：嘴唇；條件：眼妝重")]
    out, _ = NR.order_for_pack("眼妝很濃，唇色怎麼配？", [], cs)
    assert ids(out) == ["eye-heavy", "eye-only"]          # 條件 2 分 > 部位 1 分


def test_user_skin_tone_promotes_matching_original_condition():
    """M01：自述小麥色時，原文條件寫 tan 的片段往前提；dark、fair 不降權，照原名次。"""
    tan = [{"field": "skin_tone", "value": "小麥色", "raw": "我是小麥色皮膚"}]
    cs = [cand("cool-pick", 1, "條件：冷底；原文條件：If your skin has a cool undertone"),
          cand("dark", 2, "原文條件：for dark skin；on dark skin"),
          cand("fair", 3, "原文條件：If you have fair skin"),
          cand("tan-deep", 11, "原文條件：for tan, deep and dark skin"),
          cand("tan", 16, "色名：棕色；原文條件：for tan skin；on tan skin"),
          cand("tannin", 17, "原文條件：tannins in tea")]           # 只比對整個英文字
    out, log = NR.order_for_pack("我是小麥色皮膚，推薦什麼？", tan, cs)
    assert ids(out) == ["tan-deep", "tan", "cool-pick", "dark", "fair", "tannin"]
    assert log["user_skin_terms"] == ["tan"]
    assert [r["skin_match"] for r in log["order"]][:2] == [True, True]


def test_no_skin_tone_keeps_vector_rank():
    cs = [cand("a", 1, "原文條件：for tan skin"), cand("b", 2, "原文條件：for fair skin")]
    out, log = NR.order_for_pack("推薦什麼？", [], cs)
    assert ids(out) == ["a", "b"] and log["user_skin_terms"] == []


# ---------- 2. C-pp-v3 ----------

def _ans(insufficient, claims, lims=("缺資料",)):
    return PP.parse_answer(json.dumps({"insufficient": insufficient, "claims": claims, "limitations": list(lims),
                                       "clarifying_question": None}, ensure_ascii=False))


def test_insufficient_with_supported_claim_delivers_partial():
    """W04：模型標不足、但主張引用了送入片段 → 交付為部分回答並加註記。"""
    out = PP.postprocess(_ans(True, [{"text": "SYNTHETIC MLBB 指修飾自身唇色的方向", "unit_ids": ["u"]}]),
                         {"u": unit("u")}, PP.Policy(frozenset({"u"})))
    assert out["status"] == "delivered_partial" and len(out["claims"]) == 1
    assert PP.PARTIAL_NOTE in out["system_notes"] and out["limitations"] == ["缺資料"]
    assert any(f["flag"] == "model_marked_insufficient" for f in out["flags"])
    assert out["version"] == "C-pp-v5"   # C-pp-v5（轉述）只加欄位，本測試行為不變
    assert "現有來源只能回答" in PP.render_delivered(out)


def test_insufficient_with_untraceable_claim_stays_insufficient():
    out = PP.postprocess(_ans(True, [{"text": "SYNTHETIC 主張", "unit_ids": ["not-sent"]}]),
                         {"u": unit("u")}, PP.Policy(frozenset({"u"})))
    assert out["status"] == "source_insufficient" and out["claims"] == []
    assert PP.PARTIAL_NOTE not in out["system_notes"]


def test_insufficient_without_claims_unchanged():
    out = PP.postprocess(_ans(True, []), {"u": unit("u")}, PP.Policy(frozenset({"u"})))
    assert out["status"] == "source_insufficient" and out["claims"] == [] and out["flags"] == []


# ---------- 3. ask-once-v1（問答管線，測試替身模組） ----------

def _modules_with_model_question(seen):
    mods = _mock_modules(seen)
    inner = mods.postprocess

    def postprocess(parsed, units, policy):
        return {**inner.postprocess(parsed, units, policy), "clarifying_question": "SYNTHETIC 模型補問？",
                "clarifying_question_source": "model"}

    return dataclasses.replace(mods, postprocess=SimpleNamespace(parse_answer=inner.parse_answer,
                                                                 postprocess=postprocess,
                                                                 render_delivered=inner.render_delivered))


def _ask(conn, system_question):
    from app import qa
    from tests.test_qa import QUESTION, REPAIR, TAN, MockEmbedder, _keep
    seen: dict = {}
    gen = FakeGenerator(_keep(PUBLIC_KEY), _claims(PUBLIC_KEY))
    return qa.answer_question(conn, QUESTION, "user", REPAIR, embedder_factory=MockEmbedder, generator=gen,
                              explicit_conditions=TAN, pipeline_modules=_modules_with_model_question(seen),
                              system_question=system_question)


def test_model_question_suppressed_when_system_asks(conn, corpus):
    res = _ask(conn, system_question=True)
    assert res.outcome == "answered"
    assert res.log["delivered"]["clarifying_question"] is None
    assert res.log["model_question_suppressed"]["question"] == "SYNTHETIC 模型補問？"


def test_model_question_kept_without_system_question(conn, corpus):
    res = _ask(conn, system_question=False)
    assert res.log["delivered"]["clarifying_question"] == "SYNTHETIC 模型補問？"
    assert "model_question_suppressed" not in res.log


# ---------- 4. C-pp-v4（2026-10-27，T4 新程式重跑 M01） ----------

TAN = [{"field": "skin_tone", "value": "小麥色", "raw": "我是小麥色皮膚"}]


def test_source_skin_term_matches_listed_and_complexion_forms():
    assert PP.source_skin_term("小麥色", ["SYNTHETIC best colours for tan, deep and dark skin"]) == "tan skin"
    assert PP.source_skin_term("小麥色", ["SYNTHETIC won't shy away from a tan complexion"]) == "tan skin"
    assert PP.source_skin_term("小麥色", ["SYNTHETIC tannins in tea; skin care"]) is None      # 只比整個英文字
    assert PP.source_skin_term("小麥色", ["SYNTHETIC for dark skin"]) is None


def test_annotate_only_first_mention():
    assert PP.annotate_source_term("淺粉色在小麥色皮膚上顯得蒼白，小麥色也適合棕色。", "小麥色", "tan skin") ==         "淺粉色在小麥色皮膚（來源寫 tan skin）上顯得蒼白，小麥色也適合棕色。"


def test_rewritten_claim_still_checked_for_undertone_assertion():
    """改寫後仍斷言使用者底調（底調未知）→ 照舊移除，不靠改寫放行。"""
    units = {"u": unit("u", "SYNTHETIC: for tan skin, warm browns work.", conditions=["for tan skin"])}
    out = PP.postprocess(_ans(False, [{"text": "小麥色皮膚的你是暖底，適合暖棕色。", "unit_ids": ["u"]}], lims=()),
                         units, PP.Policy(frozenset({"u"}), explicit_conditions=TAN))
    assert out["claims"] == [] and out["removed"][0]["rule"] == "user_condition_inference"


# ---------- 5. reconcile-v4 補修（第四十批，T4 新程式 GAUS 判讀） ----------

def _rec(claims, lims, evidence=(), question="SYNTHETIC 問題"):
    d = {"status": "delivered", "claims": [{"claim_no": i, "text": t, "role": "unjudged"} for i, t in enumerate(claims, 1)],
         "limitations": list(lims), "removed": [], "flags": [], "system_notes": []}
    return PP.reconcile(d, question, list(evidence))


def test_evening_event_is_not_party():
    """P03 第二輪：主張只講「晚間活動」，「沒有聚會場合的直接描述」是對的，不移除。"""
    lim = "證據中沒有關於聚會場合特定唇色選擇的直接描述。"
    r = _rec(["如果是為了晚間或晚間活動，可以選擇裸色霧面唇膏搭配煙燻眼妝。", "如果您加強了腮紅或眼妝，請選擇較柔和的唇色。"], [lim])
    assert r["limitations"] == [lim]
    r2 = _rec(["聚會時可以選擇較鮮明的紅色。"], [lim])          # 主張真的講聚會 → 仍移除
    assert r2["limitations"] == [] and r2["removed"][0]["rule"] == "limitation_contradicts_claims_union"


def test_pairing_limitation_needs_one_claim():
    """P04 詞表開：「藍調紅與粉色腮紅搭配」不能用兩條各講一半的主張湊出來。"""
    lims = ["證據中未提及藍調紅與粉色腮紅搭配的具體視覺效果。"]
    split = ["如果是冷底，藍調紅是能襯托膚色的選擇。", "為了打造單色妝效果，可以將唇膏顏色與腮紅顏色搭配一致。"]
    assert _rec(split, lims)["limitations"] == lims
    one = ["藍調紅和粉色腮紅很好搭配，能打造協調的妝感。"]
    assert _rec(one, lims)["limitations"] == []


def test_feature_description_limit_checked_against_glossary_term():
    """W04 詞表關：「沒有乾燥玫瑰色的色彩特徵描述」，片段已寫 dusty rose 是藍粉色 → 移除並註記沒用到。"""
    ev = ["SYNTHETIC Blue-pinks, like fuchsia and dusty rose, are best suited for those with cool skin."]
    r = _rec(["MLBB 唇色是中等亮度、低彩度。"], ["證據中未包含乾燥玫瑰色的色彩特徵描述。"], ev)
    assert r["limitations"] == [] and r["removed"][0]["rule"] == "limitation_topic_in_evidence"
    assert r["system_notes"]
    keep = "證據中未明確定義乾燥玫瑰色與 MLBB 在色彩學上的差異。"      # 差異／色彩學不是特徵詞，照舊保留
    assert _rec(["MLBB 唇色是中等亮度、低彩度。"], [keep], ev + ["SYNTHETIC MLBB lip colours."])["limitations"] == [keep]


def test_single_claim_overlap_needs_named_concept():
    """M01 詞表開：限制講「小麥色皮膚特定底調」，主張只有小麥色、沒講底調 → 保留。"""
    lim = "證據中未提供針對小麥色皮膚特定底調的顏色建議。"
    claims = ["對於小麥色皮膚（來源寫 tan skin），最適合的唇膏顏色是色彩豐富且飽和度高的色調。"]
    assert _rec(claims, [lim])["limitations"] == [lim]
    assert _rec(["小麥色皮膚如果是暖底，可選暖棕色。"], [lim])["limitations"] == []
