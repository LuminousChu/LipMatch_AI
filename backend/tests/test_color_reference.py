"""color-ref-v1（2026-10-26）：問題提到色名時由程式附上色名參照。清冊是 SYNTHETIC，欄位同 PR029。

整支測試在檢查什麼：
1. 沒設定或指紋不符就不啟用。
2. 最長比對：「乾燥玫瑰」不會被當成「玫瑰」；單字分組詞（粉）與「奶油」不觸發。
3. 0 筆明說沒有、≤3 筆逐一列、>3 筆列總數與 3 個例子；固定說明示意、非標準、非推薦、待審。
4. API：有色名時回答附參照、日誌記對到哪些；沒色名不附。
"""
from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient

from app import color_reference as CR
from app.color_reference import ColorReference
from app.main import create_app

from .conftest import login, make_settings

HEADER = ("term_id,source_doc_id,color_term_zh,term_source_field,official_web_swatch_hex,hex_role,source_url,"
          "source_retrieved_at,snapshot_sha256,source_row_ref,review_status,quality_flags")
ROWS = [("T1", "柔霧珊瑚粉", "#f61143"), ("T2", "亮紅珊瑚", "#ed2600"), ("T3", "活力珊瑚", "#f75753"),
        ("T4", "淡珊瑚", "#e62f45"), ("T5", "奶油豆沙", "#c06b6f"), ("T6", "焦糖玫瑰", "#b65451"), ("T7", "淺粉紅", "#ffc0cb")]


def _csv(tmp_path):
    text = HEADER + "\n" + "\n".join(
        f"{t},SYN,{n},SHADENAME,{h},示意,https://www.maccosmetics.com.tw/x,2026-10-17T08:35:47Z,x,R{t},pending,"
        for t, n, h in ROWS) + "\n"
    p = tmp_path / "SYNTHETIC_color_terms.csv"
    p.write_text(text, encoding="utf-8")
    return p, hashlib.sha256(p.read_bytes()).hexdigest()


def test_disabled_without_config_or_with_wrong_sha(tmp_path):
    p, sha = _csv(tmp_path)
    assert ColorReference.from_env({})[0] is None
    cr, log = ColorReference.from_env({"COLOR_REFERENCE_CSV": str(p), "COLOR_REFERENCE_SHA256": "0" * 64})
    assert cr is None and log["reason"] == "sha256_mismatch_or_missing"
    cr, log = ColorReference.from_env({"COLOR_REFERENCE_CSV": str(p), "COLOR_REFERENCE_SHA256": sha})
    assert cr is not None and log == {"status": "enabled", "rows": 7, "sha256": sha}


def test_longest_match_and_rendering(tmp_path):
    p, sha = _csv(tmp_path)
    cr, _ = ColorReference.from_env({"COLOR_REFERENCE_CSV": str(p), "COLOR_REFERENCE_SHA256": sha})
    text, log = cr.block("MLBB 和乾燥玫瑰色、珊瑚色、豆沙色的色號各是什麼？")   # color-ref-v4：要問色號才附
    assert [m["key"] for m in log["matches"]] == ["乾燥玫瑰", "珊瑚", "豆沙"]
    assert [m["n"] for m in log["matches"]] == [0, 4, 1]
    # color-ref-v3：沒有色號的色名不列出（不寫「沒有名稱含…」）；也不被當成「玫瑰」
    assert "乾燥玫瑰" not in text and "沒有名稱含" not in text and "焦糖玫瑰" not in text
    assert "名稱含「珊瑚」的色號有 4 個" in text and "淡珊瑚" not in text         # >3 只列 3 個例子
    assert "奶油豆沙（網頁色塊 #c06b6f）" in text
    assert text.startswith("色名參照（M·A·C 台灣官網，2026-10-17 取得）") and "不是推薦" in text and "尚未審核" in text
    assert cr.block("我想要偏粉、奶油質地的唇色") == (None, None)                 # 單字與「奶油」不觸發


def test_api_appends_reference_only_when_color_mentioned(db, users, tmp_path, monkeypatch):
    p, sha = _csv(tmp_path)
    monkeypatch.setenv("COLOR_REFERENCE_CSV", str(p))
    monkeypatch.setenv("COLOR_REFERENCE_SHA256", sha)
    with TestClient(create_app(make_settings(db))) as client:
        h = login(client, "alice")
        cid = client.post("/api/conversations", headers=h, json={}).json()["id"]
        body = client.post(f"/api/conversations/{cid}/messages", headers=h, json={"question": "珊瑚色的色碼是多少？"}).json()
        assert "色名參照" in (body["answer"] or "") and "柔霧珊瑚粉" in body["answer"]
        body2 = client.post(f"/api/conversations/{cid}/messages", headers=h, json={"question": "我嘴唇容易乾怎麼辦？"}).json()
        assert "色名參照" not in (body2.get("answer") or "")


def test_v2_rewrites_no_hex_limitation_when_reference_has_swatches(tmp_path):
    """color-ref-v2（W02 保存輸出）：附了有色塊的參照時，「資料未涵蓋 HEX 色碼」改寫成固定句；沒有色號（n＝0）時不改。"""
    from app import color_reference as CR

    p, sha = _csv(tmp_path)
    cr, _ = ColorReference.from_env({"COLOR_REFERENCE_CSV": str(p), "COLOR_REFERENCE_SHA256": sha})
    _, log = cr.block("珊瑚色的色碼是多少？可以直接給我一個 HEX 嗎？")
    w02 = "提供的資料未涵蓋珊瑚色對應的具體 HEX 色碼。"                     # Gemini 3.5 W02 限制原句
    other = "商品功能未啟用：只能提供色系／妝感方向。"
    lims, changed = CR.reconcile_limitations([w02, "證據沒有色號。", other], log)
    assert lims == [CR.REWRITTEN_LIMITATION, other]                       # 兩句都改寫，只留一句
    assert changed[0] == {"before": w02, "after": CR.REWRITTEN_LIMITATION} and len(changed) == 2
    _, none_log = cr.block("乾燥玫瑰色的色碼是多少？")
    assert CR.reconcile_limitations([w02], none_log) == ([w02], [])       # 參照裡沒有色號：限制是對的，不改
    assert CR.reconcile_limitations([w02], None) == ([w02], [])
    assert CR.COLOR_REF_VERSION == "color-ref-v6"


def test_v3_no_block_when_no_shade_matches(tmp_path):
    """color-ref-v3（2026-11-02，W04）：問到的色名全部都沒有色號時整段不顯示，但日誌照記（規範 8）。"""
    p, sha = _csv(tmp_path)
    cr, _ = ColorReference.from_env({"COLOR_REFERENCE_CSV": str(p), "COLOR_REFERENCE_SHA256": sha})
    text, log = cr.block("乾燥玫瑰色適合我嗎？")
    assert text is None and log["matches"] == [{"key": "乾燥玫瑰", "n": 0, "shown": []}]


def test_v6_attached_whenever_registry_has_shade(tmp_path):
    """color-ref-v6（2026-11-08，更正 color-ref-v4）：問題提到的色名清冊裡有色號就附，不看有沒有問色號；
    W03（只比較色名）現在要附。日誌記 asks_color_value 方便追溯，不再有 question_not_asking_color_value。"""
    p, sha = _csv(tmp_path)
    cr, _ = ColorReference.from_env({"COLOR_REFERENCE_CSV": str(p), "COLOR_REFERENCE_SHA256": sha})
    for q in ("豆沙色跟奶茶色差在哪裡？", "珊瑚色適合上班嗎？",
              "珊瑚色的色碼是多少？可以直接給我一個 HEX 嗎？", "幫我挑豆沙色的相近色號"):
        text, log = cr.block(q)
        assert text and text.startswith("色名參照（") and "skipped" not in log, q
    _, log = cr.block("豆沙色跟奶茶色差在哪裡？")
    assert log["asks_color_value"] is False
    _, log = cr.block("豆沙色的色號是多少？")
    assert log["asks_color_value"] is True
    assert CR.asks_color_value("豆沙色 RGB") and not CR.asks_color_value("豆沙色好看嗎")


def test_v6_no_shade_still_hidden(tmp_path):
    """color-ref-v6 保留 color-ref-v3：問到的色名清冊裡全部沒有色號，整段不顯示，日誌記 no_shade_in_registry。"""
    p, sha = _csv(tmp_path)
    cr, _ = ColorReference.from_env({"COLOR_REFERENCE_CSV": str(p), "COLOR_REFERENCE_SHA256": sha})
    text, log = cr.block("MLBB 和乾燥玫瑰色都說很自然，兩個差在哪？")
    assert text is None and log["skipped"] == "no_shade_in_registry"


def test_v5_general_advice_only_records_reason(tmp_path):
    """color-ref-v5／v6：證據不足改附一般建議時照樣附，日誌多記 attached_reason；附不附的規則和一般情況相同。"""
    p, sha = _csv(tmp_path)
    cr, _ = ColorReference.from_env({"COLOR_REFERENCE_CSV": str(p), "COLOR_REFERENCE_SHA256": sha})
    q = "擦豆沙色比擦奶茶色更專業嗎？請給研究數據。"
    text, log = cr.block(q, general_advice=True)
    assert text and text.startswith("色名參照（") and log["attached_reason"] == "insufficient_general_advice"
    assert "skipped" not in log
    text, log = cr.block(q)
    assert text and "attached_reason" not in log
