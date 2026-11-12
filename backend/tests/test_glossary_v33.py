"""詞表 v3.3（2026-11-02）：唇釉與液態唇膏是同一種東西，共用英文檢索詞 liquid lipstick。"""
from __future__ import annotations

import pytest

from app import query_glossary as G


@pytest.mark.parametrize("text", ["唇釉要怎麼擦才持久？", "液態唇膏會不會很乾？", "液態唇膏和唇釉一樣嗎"])   # SYNTHETIC
def test_lip_lacquer_same_as_liquid_lipstick(text):
    assert G.expand(text) == ["liquid lipstick"]


def test_no_pending_lip_lacquer_term():
    assert all(t.key != "lip_lacquer" for t in (*G.TERMS, *G.REVIEW_TERMS))
    assert G.GLOSSARY_VERSION.startswith("lipmatch-query-glossary-v3.")   # v3.3 起唇釉規則不變


def test_v34_french_makeup_and_base():
    """v3.4（2026-11-03，N10）：法式妝→French makeup；「底妝」不展開（試過只增加雜訊）。"""
    assert G.GLOSSARY_VERSION in ("lipmatch-query-glossary-v3.4", "lipmatch-query-glossary-v3.5", "lipmatch-query-glossary-v3.6", "lipmatch-query-glossary-v3.7", "lipmatch-query-glossary-v3.8")   # v3.5／v3.6 只加詞條；v3.7 撤回厚重；v3.8 厚重→cakey
    got = G.expand("很多人說法式妝就是要擦大紅唇，真的一定要嗎？如果想要法式感，唇妝和底妝要怎麼處理？")
    assert got == ["French makeup"]
