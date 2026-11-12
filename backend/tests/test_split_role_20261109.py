"""split-role-v1（2026-11-09）：拆分單位沿用父段落「商品宣稱」的更正（測試組 H06）。

整支在測什麼（單位內容節錄自實驗庫 PR015_h0139／PR015_h0139#a1，屬實際資料；不連資料庫、不呼叫模型）：
  1. 正例：#a1 拆分單位、商品宣稱、沒有商品指涉 → unit_view 的 content_role 變成 None，並留下原標籤追溯。
  2. 反例：父段落（有商品名）、有商品名的拆分單位、合併單位 #m、其他角色，都照資料不變。
"""
from __future__ import annotations

from app import retrieval as RT

CHILD_TEXT = ("Applying too many layers of matte lipstick can make the color look heavy, cakey, and uneven. Instead, "
              "start with a thin, even swipe, let it set, and add only a light second layer if you need more coverage.")


def _row(key: str, role: str | None, products: list[str]) -> dict:
    """測試用資料列：模擬 context_chunks 交回的一列（support_unit 是 A 匯出的屬性）。"""
    return {"chunk_key": key, "quote_text": CHILD_TEXT, "doc_key": "PR015", "section": "Matte Lipstick Don’ts",
            "support_unit": {"content_role": role, "product_mentions": products, "conditions": [], "links": {}}}


def test_split_child_without_products_drops_product_claim():
    """PR015_h0139#a1：沒有商品名卻標商品宣稱 → 不標，保留原標籤追溯。"""
    v = RT.unit_view(_row("PR015_h0139#a1", "商品宣稱", []))
    assert v["content_role"] is None
    assert v["content_role_inherited"] == "商品宣稱"
    assert RT.SPLIT_ROLE_VERSION == "split-role-v1"


def test_other_units_keep_role():
    """反例：父段落有商品名、拆分單位有商品名、合併單位、其他角色，都照資料。"""
    assert RT.unit_view(_row("PR015_h0139", "商品宣稱", ["L’Oréal Paris Infallible"]))["content_role"] == "商品宣稱"
    assert RT.unit_view(_row("PR015_h0139#a2", "商品宣稱", ["L’Oréal Paris Infallible"]))["content_role"] == "商品宣稱"
    assert RT.unit_view(_row("PR020_h0515#m", "商品宣稱", []))["content_role"] == "商品宣稱"
    assert RT.unit_view(_row("PR015_h0139#a1", "方法", []))["content_role"] == "方法"
    assert "content_role_inherited" not in RT.unit_view(_row("PR015_h0142", "方法", []))
