"""reconcile-v7（2026-11-09，fix-metrics 第四批，開發 N01）：沒問底調卻寫底調限制 → 拿掉。

整支在測什麼（問題、限制句取自開發 N01 實際輸出；不連資料庫、不呼叫模型）：
  1. 正例：N01 只問嘴唇乾想擦霧面，限制「證據中沒有提到如何選擇適合特定膚色底調的霧面唇膏」→ 移除，記 rule。
  2. 反例：問題有提底調／冷暖（冷底、暖色調…）時保留；限制沒講底調保留；問題空白保留。
"""
from __future__ import annotations

from app import postprocess as PP

N01_Q = "我嘴唇容易乾，但又想擦霧面，有什麼辦法讓它不要看起來乾乾的？"
N01_LIMIT = "證據中沒有提到如何選擇適合特定膚色底調的霧面唇膏。"


def _delivered(limitations):
    """測試用交付物件：一條有依據的做法主張＋指定限制。"""
    return {"claims": [{"claim_no": 1, "text": "如果是嘴唇乾燥或起皮，在擦唇膏前應先進行去角質。",
                        "unit_ids": ["PR015_h0118"], "role": "direct"}],
            "limitations": list(limitations), "flags": [], "removed": [], "status": "answered"}


def test_n01_unasked_undertone_limitation_removed():
    """N01：問題沒提底調 → 底調限制不交付，留移除紀錄。"""
    out = PP.reconcile(_delivered([N01_LIMIT]), N01_Q, [])
    assert out["limitations"] == []
    assert [r["rule"] for r in out["removed"] if r.get("limitation") == N01_LIMIT] == ["limitation_unasked_undertone"]
    assert out["reconcile_version"] in ("reconcile-v7", "reconcile-v8")


def test_undertone_limitation_kept_when_asked_or_unrelated():
    """反例：問題提到冷底／暖色調時保留；限制沒講底調不受影響；問題空白保守保留。"""
    assert PP.limitation_unasked_undertone(N01_LIMIT, "我是冷底，嘴唇容易乾，霧面要怎麼擦？") is False
    assert PP.limitation_unasked_undertone(N01_LIMIT, "暖色調的霧面唇膏怎麼擦不乾？") is False
    assert PP.limitation_unasked_undertone("證據中沒有提到持妝時數。", N01_Q) is False
    assert PP.limitation_unasked_undertone(N01_LIMIT, "") is False
    assert PP.limitation_unasked_undertone(N01_LIMIT, N01_Q) is True
