"""reconcile-v8（2026-11-09，fix-metrics 第七批，驗證 P09）：限制說沒有卸除步驟，但主張就是卸除步驟 → 拿掉。

整支在測什麼（問題、主張、限制句取自驗證 P09 實際輸出；不連資料庫、不呼叫模型）：
  1. 正例：P09 限制「沒有如何卸除口紅的具體步驟或技巧，僅提到油性與水性清潔產品的順序」→ 移除，記 rule。
  2. 反例：主張沒講卸除／清潔時保留；問的是別的階段（去角質）的技巧時，照舊要有該階段做法才移除。
"""
from __future__ import annotations

from app import postprocess as PP

P09_Q = "持久型或防水的口紅很難卸，要怎麼卸才不用一直用力搓嘴唇？"
P09_LIMIT = "證據中沒有提到如何卸除口紅的具體步驟或技巧，僅提到使用油性與水性清潔產品的順序。"
P09_CLAIMS = ["如果是使用持久型或防水配方，應先使用油性清潔產品來分解色素。",
              "在沖洗掉卸妝產品後，再次使用水性清潔產品來去除殘留的油脂、妝容或雜質。"]


def _delivered(claims, limitations):
    """測試用交付物件：指定主張＋限制。"""
    return {"claims": [{"claim_no": i + 1, "text": t, "unit_ids": ["PR015_h0133"], "role": "direct"}
                       for i, t in enumerate(claims)],
            "limitations": list(limitations), "flags": [], "removed": [], "status": "answered"}


def test_p09_removal_limitation_removed():
    """P09：主張已是先油性、再水性的卸除步驟 → 限制不交付，留移除紀錄。"""
    out = PP.reconcile(_delivered(P09_CLAIMS, [P09_LIMIT]), P09_Q, [])
    assert out["limitations"] == []
    assert [r["rule"] for r in out["removed"] if r.get("limitation") == P09_LIMIT] == ["limitation_contradicts_claims_union"]
    assert out["reconcile_version"] == "reconcile-v8"


def test_removal_limitation_kept_when_claims_not_about_removal():
    """反例：主張只講持久技巧、沒講卸除／清潔 → 限制保留。"""
    claims = ["擦完唇膏後用面紙輕壓，再刷一層透明蜜粉。"]
    assert PP.limitation_topic_covered("證據中沒有提到如何卸除口紅的具體步驟或技巧。", claims) is None


def test_stage_technique_still_needs_that_stage():
    """反例：限制問去角質的技巧，主張只說先去角質（沒有做法手段）→ 照舊保留。"""
    claims = ["在擦唇膏前先去角質，再擦護唇膏。"]
    assert PP.limitation_topic_covered("證據中沒有提到去角質的具體技巧。", claims) is None
