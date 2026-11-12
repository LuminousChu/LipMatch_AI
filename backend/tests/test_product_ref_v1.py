"""product-ref-v1（2026-11-07）：11-01 商品界線落實到回答——品牌色當顏色參考可以提、不推薦購買。

檢查：
  1. 開關關掉（預設）時提示與交付和原本相同（商品名照舊拿掉）。
  2. 開了以後：當顏色參考的商品／色號名保留，交付附「只供顏色參考」說明。
  3. 叫人買、商品宣稱（持妝幾小時）就算開了也照舊處理。
  4. 提示第 6 條只在開了時換成參考版。
  5. QaConfig 讀環境變數 QA_PRODUCT_REFERENCE。
"""
from __future__ import annotations

import json

from app import answer_policy as AP
from app import postprocess as PP
from app import qa

ROSE = "Rose to Fame"
UNIT_TEXT = ("My Rose to Fame Lipstick truly blossoms on a deeper complexion. This soft and muted mauve lipstick "
             "looks rosy and romantic.")


def unit(uid, text=UNIT_TEXT, mentions=(ROSE,)):
    """SYNTHETIC 測試單位：含一個商品名。"""
    return {"unit_id": uid, "text": text, "conditions": [], "content_role": None, "support_scope": None,
            "product_mentions": list(mentions)}


def run(text, product_reference):
    """用一條主張跑後處理。"""
    raw = json.dumps({"insufficient": False, "claims": [{"text": text, "unit_ids": ["u"], "conditions": []}],
                      "limitations": [], "clarifying_question": None}, ensure_ascii=False)
    units = {"u": unit("u")}
    pol = PP.Policy(kept_unit_ids=frozenset(units), product_reference=product_reference)
    return PP.postprocess(PP.parse_answer(raw), units, pol)


REF_CLAIM = "如果是深膚色，可以考慮柔和帶灰的藕紫色；來源舉的例子：Rose to Fame 是一種柔和的玫瑰粉。"


def test_off_keeps_old_behaviour():
    """開關關掉：商品名照舊被拿掉或整句不交付，不出現參考說明。"""
    out = run(REF_CLAIM, product_reference=False)
    assert all(ROSE not in c["text"] for c in out["claims"])
    assert PP.PRODUCT_REF_NOTE not in out["system_notes"]
    assert out["product_reference"] == "off"


def test_on_keeps_reference_and_adds_note():
    """開了：當顏色參考的色號名保留原句，附說明。"""
    out = run(REF_CLAIM, product_reference=True)
    assert [c["text"] for c in out["claims"]] == [REF_CLAIM]
    assert out["claims"][0]["product_reference"]
    assert PP.PRODUCT_REF_NOTE in out["system_notes"]
    assert any(f["flag"] == "product_reference_kept" for f in out["flags"])


def test_on_still_blocks_buying_and_product_claims():
    """開了：叫人買、商品宣稱（持妝 16 小時）照舊處理。"""
    buy = run("推薦你買 Rose to Fame，顏色柔和。", product_reference=True)
    assert all(ROSE not in c["text"] for c in buy["claims"])
    claim = run("Rose to Fame 持妝 16 小時，顏色柔和。", product_reference=True)
    assert all(ROSE not in c["text"] for c in claim["claims"])


def test_prompt_rule6_switches_only_when_on():
    """提示第 6 條：關掉時與原本逐字相同；開了換成參考版、版本號加上 product-ref-v1。"""
    units = {"u": {**unit("u"), "title": "SYNTHETIC", "locator_primary": None, "section_original": None}}
    sel = {"selector": "none", "kept_unit_ids": ["u"], "items": [], "status": "ok"}
    off = AP.build_answer_prompt("深膚色可以擦粉色嗎？", [], sel, units)
    on = AP.build_answer_prompt("深膚色可以擦粉色嗎？", [], sel, units, product_reference=True)
    assert off.messages[0]["content"].startswith(AP.SYSTEM_PROMPT[:30])
    assert AP.RULE6_PRODUCT_OLD in off.messages[0]["content"]
    assert AP.RULE6_PRODUCT_REF in on.messages[0]["content"] and AP.RULE6_PRODUCT_OLD not in on.messages[0]["content"]
    assert on.version.endswith("+" + AP.PRODUCT_REF_VERSION) and not off.version.endswith(AP.PRODUCT_REF_VERSION)


def test_config_reads_env(monkeypatch):
    """QA_PRODUCT_REFERENCE：預設 off；on 打開；其他值報錯。"""
    monkeypatch.delenv("QA_PRODUCT_REFERENCE", raising=False)
    assert qa.QaConfig.from_env().product_reference is False
    monkeypatch.setenv("QA_PRODUCT_REFERENCE", "on")
    assert qa.QaConfig.from_env().product_reference is True
    monkeypatch.setenv("QA_PRODUCT_REFERENCE", "yes")
    try:
        qa.QaConfig.from_env()
    except RuntimeError:
        pass
    else:
        raise AssertionError("QA_PRODUCT_REFERENCE=yes 應該報錯")


def test_v2_at_most_three_items_codes_not_counted():
    """product-ref-v2：第 4 個品項改回原本處理；色碼不算品項。"""
    names = ["Alpha Rose", "Beta Plum", "Gamma Coral", "Delta Berry"]
    units = {f"u{i}": unit(f"u{i}", text=f"SYNTHETIC {n} is a soft shade.", mentions=(n,)) for i, n in enumerate(names)}
    # product-ref-v4（2026-11-08）：參考句要看得出色相，假句子由「柔和的顏色」改「柔和的粉色」（本測試只驗品項上限）
    claims = [{"text": f"來源舉的例子：{n} 是一種柔和的粉色。", "unit_ids": [f"u{i}"], "conditions": []}
              for i, n in enumerate(names)]
    raw = json.dumps({"insufficient": False, "claims": claims, "limitations": [], "clarifying_question": None},
                     ensure_ascii=False)
    out = PP.postprocess(PP.parse_answer(raw), units, PP.Policy(kept_unit_ids=frozenset(units), product_reference=True))
    kept = [x for c in out["claims"] for x in c.get("product_reference") or []]
    assert kept == names[:3]
    assert any(f["flag"] == "product_reference_over_limit" for f in out["flags"])
    assert all("Delta Berry" not in c["text"] for c in out["claims"])
    assert PP.product_items(["#ed2600", "212", "MA 012", "Rose to Fame", "Rose to Fame"]) == ["Rose to Fame"]


def test_v3_suggesting_a_product_is_recommendation():
    """product-ref-v3：「建議選擇 某商品」是推薦，不是參考（prodref 40 題 N02）。"""
    out = run("若偏好霧面，建議選擇 Rose to Fame。", product_reference=True)
    assert all(ROSE not in c["text"] for c in out["claims"])
    out2 = run("來源舉的例子：Rose to Fame 是一種柔和的玫瑰粉。", product_reference=True)
    assert out2["claims"] and ROSE in out2["claims"][0]["text"]
