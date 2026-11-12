"""citation-label-v1 出處文字測試（2026-11-01）。

使用者指出回答只顯示文件編號（PR021、〔來源 1〕）沒有意義；改為「〈文章標題〉，刊物／學校／網站，年份，p.頁碼」。
這裡只測純函式（不連資料庫、不呼叫模型）；書目內容是為測試寫的（SYNTHETIC），不是正式文獻資料。
"""
from __future__ import annotations

from app import qa
from app import recommend as rec
from app import source_label as SL

BIB = {"title": "SYNTHETIC Lip Study", "institution": "SYNTHETIC Journal of Colour, 1, 1–10", "year": 2025}


# 期刊論文：標題、刊物、年份、頁碼全部寫出；不出現文件編號
def test_label_full_paper():
    text = SL.label("SYNTHETIC Lip Study", SL.venue_of(BIB), SL.year_of(BIB), 4, 4)
    assert text == "〈SYNTHETIC Lip Study〉，SYNTHETIC Journal of Colour, 1, 1–10，2025，p.4"


# 跨頁寫成 p.66–67；單頁 page_end 為 None 也只寫一頁
def test_page_range_and_single():
    assert SL.page_text(66, 67) == "p.66–67"
    assert SL.page_text(5, None) == "p.5"
    assert SL.page_text(None, None) is None


# 網頁文章：書目沒有年份、片段沒有頁碼 → 兩者都省略，不推測、不顯示網頁段落定位
def test_label_web_article_without_year_or_page():
    bib = {"institution": "SYNTHETIC Beauty Magazine", "year": None}
    text = SL.label("SYNTHETIC How To", SL.venue_of(bib), SL.year_of(bib), None, None)
    assert text == "〈SYNTHETIC How To〉，SYNTHETIC Beauty Magazine"


# 書目欄位空白或缺漏時視為沒有；標題缺時寫明未登錄，不改用文件編號
def test_missing_fields():
    assert SL.venue_of({"institution": "  "}) is None and SL.venue_of(None) is None
    assert SL.year_of({}) is None
    assert SL.label(None) == "（未登錄標題的來源）"


# label_of 從引用物件取欄位；with_page=False 用於「出處：」總覽行
def test_label_of_citation_dict():
    cit = {"doc_key": "SYN_DOC", "title": "SYNTHETIC Lip Study", "venue": "SYNTHETIC Journal", "year": 2025,
           "page_start": 3, "page_end": 3}
    assert SL.label_of(cit) == "〈SYNTHETIC Lip Study〉，SYNTHETIC Journal，2025，p.3"
    assert SL.label_of(cit, with_page=False) == "〈SYNTHETIC Lip Study〉，SYNTHETIC Journal，2025"
    assert "SYN_DOC" not in SL.label_of(cit)


# 問答本文：〔來源 unit_id〕換成出處文字；同一出處重複只寫一次；查不到的 id 原樣保留
def test_qa_label_sources():
    refs = {"u1": "〈A〉，J，p.1", "u2": "〈A〉，J，p.1", "u3": "〈B〉，K"}
    text = qa._label_sources("句一〔來源 u1、u2〕\n句二〔來源 u3、zz〕", refs)
    assert text == "句一〔來源：〈A〉，J，p.1〕\n句二〔來源：〈B〉，K；zz〕"


# 舊格式退回：全部只有編號時維持〔來源 1、2〕
def test_qa_source_ref_numbers_fallback():
    assert qa._source_ref(["1", "2"]) == "〔來源 1、2〕"


# 推薦證據的引用帶出刊物與年份（給前端與推薦文字用）
def test_recommend_citation_has_venue_and_year():
    ev = rec.Evidence(chunk_id=1, chunk_key="SYN_c1", doc_key="SYN_DOC", version_label="v1", title="SYNTHETIC T",
                      page_start=2, page_end=2, html_locator=None, support_type="styling", claim="SYNTHETIC",
                      supports=[], valid=True, venue="SYNTHETIC Journal", year=2025)
    cit = ev.citation()
    assert cit["venue"] == "SYNTHETIC Journal" and cit["year"] == 2025
    assert SL.label_of(cit) == "〈SYNTHETIC T〉，SYNTHETIC Journal，2025，p.2"
