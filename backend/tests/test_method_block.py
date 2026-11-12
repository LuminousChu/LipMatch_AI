"""結構化方法呈現（method-block-app-v1）：關係選段的方法組展開、方法區塊組裝、拒收時攔下模型句子。
SYNTHETIC 關係包與合成片段（不是來源原文或驗收）；經 API 的一題沿用 test_relation_select 的 MOCK 替身。"""
from __future__ import annotations

import copy
import hashlib

from app import method_block as MB, qa, relation_select as RS
from tests.test_qa import PUBLIC_KEY, QUESTION, FakeGenerator, _ask, _claims, _mock_modules
from tests.test_qa import conn, corpus, make_client  # noqa: F401  （沿用 test_qa 的資料庫 fixture）
from tests.test_relation_select import SHA_Q, _config, _log, _pack

OTHER = "SYN_OTHER_h0002"
OTHER_TEXT = "SYNTHETIC 材料：非乳膠海綿。"
SHA_O = hashlib.sha256(OTHER_TEXT.encode("utf-8")).hexdigest()


def _group(units=(PUBLIC_KEY, OTHER), conditions=(), exportable=True, complete=True):
    return {"relation_id": "SYN-R1", "entry_unit": PUBLIC_KEY, "units": list(units), "required_units": list(units),
            "unit_text_sha256": {PUBLIC_KEY: SHA_Q, OTHER: SHA_O}, "exportable": exportable,
            "reason": None if exportable else "段落超出核准範圍",
            "block": {"evidence_level": "SYNTHETIC",
                      "applies_context": [{"quote": "如果唇色偏深", "unit_id": PUBLIC_KEY, "required": True}],
                      "material": [{"quote": "非乳膠海綿", "unit_id": OTHER, "required": True}],
                      "steps": [{"order": 1, "action": "先上底", "quote": "明度", "required": True, "unit_id": PUBLIC_KEY}],
                      "purpose": [], "usage_note": [], "boundaries": ["沒有指定色號"],
                      "boundaries_hidden_internal": ["dim=value 內部註記"], "relation_id": "SYN-R1"},
            "completeness": {"complete": complete, "missing": [] if complete else ["步驟 2"]},
            "conditions": list(conditions)}


def _mpack(group=None):
    p = _pack()
    p["method_groups"] = {"SYN-1": group or _group()}
    return p


def _cands():
    return [{"unit_id": PUBLIC_KEY, "text": QUESTION, "pool_rank": 1}, {"unit_id": OTHER, "text": OTHER_TEXT, "pool_rank": 2}]


SENT = {PUBLIC_KEY: SHA_Q, OTHER: SHA_O}
CLAIMS = [{"text": "模型寫的方法", "unit_ids": [PUBLIC_KEY]}, {"text": "組外的補充", "unit_ids": ["SYN_FREE"]}]


# ---------------------------------------------------------------------------
# 關係選段的方法組展開
# ---------------------------------------------------------------------------
def test_select_expands_group_units_from_candidates():
    r = RS.select(QUESTION, _cands(), _mpack())
    assert r["kept"] == [PUBLIC_KEY, OTHER] and r["method_group"]["status"] == "expanded"


def test_select_does_not_expand_when_group_unit_missing_from_candidates():
    r = RS.select(QUESTION, _cands()[:1], _mpack())
    assert r["kept"] == [PUBLIC_KEY]
    assert r["method_group"] == {"entry": "SYN-1", "status": "not_expanded", "missing_units": [OTHER], "reason": None}


def test_select_without_method_groups_is_unchanged():
    r = RS.select(QUESTION, _cands(), _pack())
    assert r["kept"] == [PUBLIC_KEY] and r["method_group"] is None


# ---------------------------------------------------------------------------
# 方法區塊組裝
# ---------------------------------------------------------------------------
def _sel(pack, cands=None):
    return RS.select(QUESTION, cands or _cands(), pack)


def test_complete_group_replaces_model_claims_citing_group():
    pack = _mpack()
    out = MB.assemble(_sel(pack), pack, CLAIMS, ["模型限制"], None, SENT)
    text = "\n".join(out["lines"])
    assert out["status"] == "complete" and out["lines"][0] == MB.HEADER
    assert "材料限定" in text and "1. 先上底（標註者轉述）" in text and f"〔來源 {OTHER}〕" in text
    assert "模型寫的方法" not in text and "組外的補充〔來源 SYN_FREE〕" in text
    assert out["replaced_claims"] == [CLAIMS[0]] and out["blocked_claims"] == []
    assert out["cited_units"] == [PUBLIC_KEY, OTHER, "SYN_FREE"]


def test_incomplete_group_says_what_is_missing():
    pack = _mpack(_group(complete=False))
    out = MB.assemble(_sel(pack), pack, CLAIMS, [], None, SENT)
    assert out["status"] == "incomplete" and any("缺：步驟 2" in x for x in out["lines"])


def test_required_condition_conflict_rejects_and_blocks_model_claims():
    warm = {"tree": {"all": [{"dim": "undertone", "eq": "warm", "quote": "暖底"}]}, "required": True, "from": "SYN-R1",
            "quotes": ["暖底"]}
    pack = _mpack(_group(conditions=[warm]))
    sel = RS.select("我是冷底，" + QUESTION, _cands(), pack)
    out = MB.assemble(sel, pack, CLAIMS, [], None, SENT)
    text = "\n".join(out["lines"])
    assert out["status"] == "rejected" and "衝突" in out["reason"]
    assert "模型寫的方法" not in text and out["blocked_claims"] == [CLAIMS[0]]
    assert PUBLIC_KEY not in out["cited_units"]


def test_unknown_condition_shows_method_and_asks_condition():
    warm = {"tree": {"all": [{"dim": "undertone", "eq": "warm", "quote": "暖底"}]}, "required": True, "from": "SYN-R1",
            "quotes": ["暖底"]}
    pack = _mpack(_group(conditions=[warm]))
    out = MB.assemble(_sel(pack), pack, CLAIMS, [], "要不要推薦色號？", SENT)
    assert out["status"] == "complete" and out["condition_status"] == "未知"
    assert any("條件未知" in x for x in out["lines"]) and "暖底" in out["clarifying_question"]


def test_missing_or_changed_group_unit_rejects():
    pack = _mpack()
    assert MB.assemble(_sel(pack), pack, CLAIMS, [], None, {PUBLIC_KEY: SHA_Q})["status"] == "rejected"
    out = MB.assemble(_sel(pack), pack, CLAIMS, [], None, {PUBLIC_KEY: SHA_Q, OTHER: "0" * 64})
    assert out["status"] == "rejected" and "原文" in out["reason"]


def test_not_exportable_group_rejects_and_product_question_hidden():
    pack = _mpack(_group(exportable=False))
    out = MB.assemble(_sel(pack, _cands()), pack, CLAIMS, [], "推薦哪一支口紅？", SENT)
    assert out["status"] == "rejected" and "核准範圍" in out["reason"] and out["clarifying_question"] is None


def test_no_group_is_passthrough():
    pack = _pack()
    assert MB.assemble(_sel(pack), pack, CLAIMS, [], None, SENT)["status"] == "passthrough"


# ---------------------------------------------------------------------------
# 經 API：方法組只有 PUBLIC 片段（SYNTHETIC 庫只有這段可用）時，回答由方法區塊取代
# ---------------------------------------------------------------------------
def test_method_block_through_api(make_client, db, corpus, monkeypatch, tmp_path):
    seen: dict = {}
    monkeypatch.setattr(qa, "load_pipeline_modules", lambda: _mock_modules(seen))
    gen = FakeGenerator(_claims(PUBLIC_KEY, text="模型自己寫的步驟"))
    g = _group(units=(PUBLIC_KEY,))
    g["block"]["material"] = []
    body = _ask(make_client(gen, config=_config(tmp_path, _mpack(copy.deepcopy(g)))))
    assert body["status"] == "answered" and body["answer"].startswith(MB.HEADER)
    assert "模型自己寫的步驟" not in body["answer"] and "〔來源：〈" in body["answer"] and "〔來源 1〕" not in body["answer"]
    log = _log(db, body["request_id"])
    assert log["method_block"]["status"] == "complete" and log["method_block"]["model_calls"] == 0
    assert log["relation_selection"]["method_group"]["status"] == "expanded"


def test_block_keeps_non_item_fields_and_hides_internal_notes():
    """匯出的方法區塊含字串清單（隱藏的內部註記）與純文字欄位：只過濾逐項附來源的欄位，其他原樣保留、不顯示內部註記。
    （2026-10-25 整合驗收實跑時，字串清單被當成項目處理而出錯，補這個測試。）"""
    pack = _mpack()
    out = MB.assemble(_sel(pack), pack, CLAIMS, [], None, SENT)
    text = chr(10).join(out["lines"])
    assert out["status"] == "complete" and "內部註記" not in text and "沒有指定色號" in text


def test_background_prep_application_and_section_step_rendered_and_cited():
    """N01 型方法組：為什麼／事前準備／擦法三區塊，原文在小節標題的步驟標明來源；三區塊的片段都列入引用。"""
    g = _group()
    g["block"]["background"] = [{"quote": "霧面會凸顯唇紋", "unit_id": OTHER, "required": False}]
    g["block"]["prep"] = [{"quote": "先去角質", "unit_id": OTHER, "required": True}]
    g["block"]["application"] = [{"quote": "先薄擦一層", "unit_id": PUBLIC_KEY, "required": False}]
    g["block"]["steps"].append({"order": 2, "action": "擦護唇膏", "quote": "Don't skip lip balm", "required": "must",
                                "unit_id": PUBLIC_KEY, "quote_source": "section"})
    g["block"]["steps"].append({"order": 3, "action": "（可選）擦保濕打底", "quote": "you can also layer on", "required": "optional",
                                "unit_id": PUBLIC_KEY})
    pack = _mpack(g)
    out = MB.assemble(_sel(pack), pack, CLAIMS, [], None, SENT)
    text = chr(10).join(out["lines"])
    for head in ("為什麼會這樣（作者說法）：", "事前準備：", "擦法："):
        assert head in text
    assert text.count("（可選）") == 1   # 標註者轉述已寫（可選）時不重複加（N01 實跑發現）
    assert "（原文為小節標題）" in text and text.index("事前準備") < text.index("步驟") < text.index("擦法")
    assert set(out["cited_units"]) >= {PUBLIC_KEY, OTHER}



def test_brief_shows_conclusion_and_steps_without_quotes_and_keeps_audit():
    """brief-v1（使用者 2026-10-25 真人判定）：使用者只看一句結論＋中文做法；原文與逐項來源留在 audit_lines，
    引用片段不變（前端放進「參考依據」）。"""
    g = _group()
    g["brief"] = {"conclusion": "SYNTHETIC 結論一句。", "steps": ["先上底。", "再擦口紅。"], "note": "不必每天做。"}
    pack = _mpack(g)
    out = MB.assemble(_sel(pack), pack, CLAIMS, [], None, SENT)
    assert out["brief"] is True and out["lines"][:4] == ["SYNTHETIC 結論一句。", "做法：", "1. 先上底。", "2. 再擦口紅。"]
    text = "\n".join(out["lines"])
    assert "原文" not in text and MB.HEADER not in text and "不必每天做。" in text
    assert out["audit_lines"][0] == MB.HEADER and set(out["cited_units"]) >= {PUBLIC_KEY, OTHER}


def test_brief_not_used_when_group_rejected():
    g = _group(exportable=False)
    g["brief"] = {"conclusion": "SYNTHETIC 結論。", "steps": ["步驟。"]}
    pack = _mpack(g)
    out = MB.assemble(_sel(pack), pack, CLAIMS, [], None, SENT)
    assert out["status"] == "rejected" and out["brief"] is False and "SYNTHETIC 結論。" not in "\n".join(out["lines"])


def test_brief_drops_model_followup_question():
    g = _group()
    g["brief"] = {"conclusion": "SYNTHETIC 結論。", "steps": ["步驟。"]}
    pack = _mpack(g)
    out = MB.assemble(_sel(pack), pack, CLAIMS, [], "您是否願意嘗試這個方法？", SENT)
    assert out["clarifying_question"] is None and "想再確認" not in "\n".join(out["lines"])
