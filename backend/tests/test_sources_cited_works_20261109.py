"""sources-cited-v1（2026-11-09）：來源頁的「轉引文獻」一律回傳字串清單。

整支在測什麼：
  1. cited_works_list：清單照用；JSON／Python 寫法的清單文字解析；空值、"[]" 回 []；其他文字當單筆。
  2. API：片段的 attributes.cited_works 存成文字（實驗資料庫實際出現的寫法）時，
     /api/sources/chunks（含關鍵字搜尋）回傳的仍是陣列，前端不會因呼叫 join 而整頁顯示錯誤。
"""
from __future__ import annotations

import psycopg

from app.main import cited_works_list

from .conftest import login
from .test_ingest import DOC
from .test_ui_api import _approve_and_publish, imported  # noqa: F401  imported 是 fixture


def test_cited_works_list_normalizes_values():
    """各種存法都轉成字串清單。"""
    assert cited_works_list(None) == []
    assert cited_works_list([]) == []
    assert cited_works_list(["[3]", "Tian, 2023"]) == ["[3]", "Tian, 2023"]
    assert cited_works_list("[]") == []
    assert cited_works_list("['Liang and Wan, 2017', 'Li et al., 2017']") == ["Liang and Wan, 2017", "Li et al., 2017"]
    assert cited_works_list('["Smith, 2020"]') == ["Smith, 2020"]
    assert cited_works_list("Smith, 2020") == ["Smith, 2020"]
    assert cited_works_list("  ") == []


def test_sources_search_returns_list_when_stored_as_text(client, imported):  # noqa: F811
    """cited_works 存成文字時，列表與搜尋都回傳陣列。"""
    keys = [f"{DOC}_p0001_b001", f"{DOC}_p0001_b002"]
    # 發布後片段內容不可改（資料庫保護），所以在草稿階段先把欄位寫成文字，再核准發布
    with psycopg.connect(imported, autocommit=True) as conn:   # imported 是測試資料庫連線字串
        for key, text in ((keys[0], "['Liang and Wan, 2017']"), (keys[1], "[]")):
            conn.execute("UPDATE chunks SET attributes = jsonb_set(attributes, '{cited_works}', to_jsonb(%s::text))"
                         " WHERE chunk_key = %s", (text, key))
    _approve_and_publish(imported, keys)
    h = login(client, "alice")
    items = client.get("/api/sources/chunks", headers=h).json()["items"]
    got = {i["chunk_key"]: i["cited_works"] for i in items}
    assert got == {keys[0]: ["Liang and Wan, 2017"], keys[1]: []}
    hits = client.get("/api/sources/chunks", params={"q": "merged"}, headers=h).json()["items"]
    assert all(isinstance(i["cited_works"], list) for i in hits)
