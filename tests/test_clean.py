"""02_clean 的測試：單元測試（純函式）+ 學術 PDF 與彩妝 HTML 的端到端測試。"""

from __future__ import annotations

import hashlib
import json

import pytest

from schemas import Document, SourceManifest, TextBlock
from tests.conftest import HTML_FIXTURE, LEEDS_PDF

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def make_block(i: int, raw: str, block_type: str = "paragraph", page: int | None = 1, **kw) -> TextBlock:
    return TextBlock(doc_id="t", block_id=f"t_b{i:03d}", block_type=block_type, order=i,
                     page_number=page, raw_text=raw, **kw)


def make_doc(blocks: list[TextBlock], source_type: str = "pdf") -> Document:
    m = SourceManifest(doc_id="t", source_path="x", source_type=source_type, sha256="0" * 64,
                       file_size_bytes=0, extractor="test")
    return Document(stage="extracted", manifest=m, blocks=blocks)


# ---------------------------------------------------------------------------
# 斷行
# ---------------------------------------------------------------------------


def test_joins_wrapped_pdf_lines(clean_mod):
    raw = "The candidate confirms that the work \nsubmitted is her own, except where \nwork has formed part."
    out, ops = clean_mod.fix_pdf_line_breaks(raw)
    assert out == "The candidate confirms that the work submitted is her own, except where work has formed part."
    assert "joined_line_breaks" in ops


def test_line_end_hyphen_is_kept_for_compound_words(clean_mod):
    # 論文 p.3、p.12 的真實例子：self-esteem、black-body 都是複合詞，不能刪連字號
    out, ops = clean_mod.fix_pdf_line_breaks("broken self-\nesteem together\nto a black-\nbody radiator")
    assert out == "broken self-esteem together to a black-body radiator"
    assert "joined_hyphenated_word_kept_hyphen" in ops


def test_dehyphenate_option(clean_mod):
    out, ops = clean_mod.fix_pdf_line_breaks("colour measure-\nment", dehyphenate=True)
    assert out == "colour measurement" and "dehyphenated" in ops


def test_spaced_dash_and_cielab_axes(clean_mod):
    out, _ = clean_mod.fix_pdf_line_breaks("lightness – chroma and a* -\nb* values")
    assert out == "lightness – chroma and a* - b* values"


def test_broken_url_rejoined(clean_mod):
    out, ops = clean_mod.fix_pdf_line_breaks(
        "types \nhttps://en.wikipedia.org/wiki/CIE_1931_color_space#/media/File:Cones_SMJ2\n_E.svg)")
    assert out.endswith("File:Cones_SMJ2_E.svg)")
    assert "joined_broken_url" in ops


def test_cjk_lines_joined_without_space(clean_mod):
    out, ops = clean_mod.fix_pdf_line_breaks("唇膏顏色會透過色彩同化\n改變膚色感知。\nCIE Lab 數值")
    assert out == "唇膏顏色會透過色彩同化改變膚色感知。 CIE Lab 數值"
    assert "joined_cjk_without_space" in ops


# ---------------------------------------------------------------------------
# Unicode：不可以破壞公式
# ---------------------------------------------------------------------------


def test_normalize_unicode_fixes_layout_chars(clean_mod):
    out, ops = clean_mod.normalize_unicode("eﬃcient D65 colou­r​")
    assert out == "efficient D65 colour"
    assert {"expanded_ligatures", "normalized_special_spaces", "removed_invisible_chars"} <= set(ops)


def test_normalize_unicode_keeps_math_symbols(clean_mod):
    formula = "ΔE*ab = √[(ΔL*)² + (Δa*)² + (Δb*)²], 𝐿 𝑎 𝑏, h° ± 0.5, b* = −3.12"
    out, _ = clean_mod.normalize_unicode(formula)
    assert out == formula  # 若誤用 NFKC，² 會變成 2、𝐿 會變成 L


# ---------------------------------------------------------------------------
# 內容保全：CIELAB 數值、公式、研究條件
# ---------------------------------------------------------------------------

CIELAB_PARAGRAPH = (
    "Skin colour was measured under D65 (10° observer, n = 782 samples):\n"
    "L* = 62.35, a* = 14.20, b* = 18.95, C*ab = 23.68, h = 53.2°;\n"
    "mean ΔE00 = 2.2 units and ΔE*ab = √(ΔL*² + Δa*² + Δb*²) < 3.0, p < 0.05."
)


def test_cielab_values_and_formula_survive_cleaning(clean_mod):
    cleaned, _ = clean_mod.clean_text(CIELAB_PARAGRAPH, "paragraph", "pdf")
    for token in ["D65", "10°", "n = 782", "L* = 62.35", "a* = 14.20", "b* = 18.95", "C*ab = 23.68",
                  "53.2°", "ΔE00 = 2.2", "√(ΔL*² + Δa*² + Δb*²) < 3.0", "p < 0.05"]:
        assert token in cleaned, token
    assert clean_mod.verify_content_preserved(CIELAB_PARAGRAPH, cleaned) == []


def test_verify_content_detects_lost_numbers(clean_mod):
    missing = clean_mod.verify_content_preserved("L* = 62.35, a* = 14.20", "L* = 62.35")
    assert "a*" in missing and "14.20" in missing


def test_clean_block_falls_back_to_raw_when_content_lost(clean_mod, monkeypatch):
    # 模擬一個有 bug 的清洗步驟把數值吃掉 → 必須退回原文並標記人工確認
    monkeypatch.setattr(clean_mod, "clean_text", lambda t, bt, st, **kw: (t.split(",")[0], ["buggy"]))
    blk = make_block(0, "L* = 62.35, a* = 14.20, b* = 18.95")
    out, issues = clean_mod.clean_block(blk, "pdf", None)
    assert out.cleaned_text == "L* = 62.35, a* = 14.20, b* = 18.95"
    assert out.needs_review and issues and issues[0].issue_type == "content_check_failed"


def test_table_cells_cleaned_without_changing_shape(clean_mod):
    rows = [["Shade", "L*", "a*", "b*"], ["Dark\nPink", " 45.32 ", "28.71", None], ["Purple", "30.25", "35.48", "−3.12"]]
    out = clean_mod.clean_table(rows)
    assert out == [["Shade", "L*", "a*", "b*"], ["Dark Pink", "45.32", "28.71", None], ["Purple", "30.25", "35.48", "−3.12"]]
    assert [len(r) for r in out] == [len(r) for r in rows]


# ---------------------------------------------------------------------------
# 頁首頁尾
# ---------------------------------------------------------------------------


def test_repeated_header_and_page_numbers_removed_but_footnote_kept(clean_mod):
    blocks, i = [], 0
    for p in range(1, 6):
        blocks.append(make_block(i, "Investigation on Lipstick Shades — Y. Chen", "page_header", p)); i += 1
        blocks.append(make_block(i, f"Body text of page {p} with L* = 6{p}.0", "paragraph", p)); i += 1
        blocks.append(make_block(i, f"{p} ", "page_footer", p)); i += 1
    blocks.append(make_block(i, "¹ Measured with a CS-2000 at 1° field of view.", "page_footer", 5))
    cleaned = clean_mod.clean_document(make_doc(blocks))

    by_id = {b.block_id: b for b in cleaned.blocks}
    headers = [b for b in cleaned.blocks if b.block_type == "page_header"]
    numbers = [b for b in cleaned.blocks if b.block_type == "page_footer" and b.raw_text.strip().isdigit()]
    assert all(b.is_removed and b.removal_reason.startswith("page_header:repeated") for b in headers)
    assert all(b.is_removed and b.removal_reason == "page_footer:page_number" for b in numbers)
    footnote = by_id[f"t_b{i:03d}"]
    assert not footnote.is_removed and "CS-2000" in footnote.cleaned_text
    # raw_text 不得被動到
    assert all(b.raw_text == o.raw_text for b, o in zip(cleaned.blocks, blocks))
    assert all(not b.is_removed for b in cleaned.blocks if b.block_type == "paragraph")


def test_cross_page_paragraph_linked(clean_mod):
    blocks = [
        make_block(0, "CIE standard D65 light source, with a CCT", page=12),
        make_block(1, "12", "page_footer", page=12),
        make_block(2, "of 6504 K, represents average daylight.", page=13),
    ]
    cleaned = clean_mod.clean_document(make_doc(blocks))
    assert cleaned.blocks[2].continues_from_block_id == "t_b000"
    assert cleaned.blocks[2].cleaned_text == "of 6504 K, represents average daylight."  # 不合併文字


# ---------------------------------------------------------------------------
# search_text
# ---------------------------------------------------------------------------


def test_search_text_normalizes_math_glyphs_but_cleaned_text_keeps_them(clean_mod):
    raw = "Where ∆𝐿∗, ∆𝑎∗ and ∆𝑏∗ describe the colour difference, and 𝑥² is −1."
    doc = clean_mod.clean_document(make_doc([make_block(0, raw)]))
    b = doc.blocks[0]
    assert b.cleaned_text == raw                       # 公式字元原樣保留
    assert b.search_text == "Where ΔL*, Δa* and Δb* describe the colour difference, and x2 is -1."
    assert "search:nfkc" in b.cleaning_ops


def test_search_text_splits_glued_term_only_when_rest_is_a_document_word(clean_mod):
    # p.14 原文就是 "CIE Laboffers"／"CIE Labcolour"（作者漏空格）；Laboratory 不能被拆
    blocks = [make_block(0, "Compared to XYZ, CIE Laboffers a direct expression in CIE Labcolour space."),
              make_block(1, "It offers colour data measured in the Laboratory.")]
    doc = clean_mod.clean_document(make_doc(blocks))
    a, b = doc.blocks
    assert "Laboffers" in a.cleaned_text               # 原文錯誤照原樣保留
    assert a.search_text == "Compared to XYZ, CIE Lab offers a direct expression in CIE Lab colour space."
    assert "search:split_glued_term" in a.cleaning_ops
    assert "Laboratory" in b.search_text and "search:split_glued_term" not in b.cleaning_ops


def test_search_text_joins_preserved_line_breaks(clean_mod):
    doc = clean_mod.clean_document(make_doc([make_block(0, "𝐿∗= 116\n𝑌\n𝑌!", block_type="formula")]))
    assert doc.blocks[0].cleaned_text == "𝐿∗= 116\n𝑌\n𝑌!"
    assert doc.blocks[0].search_text == "L*= 116 Y Y!"


# ---------------------------------------------------------------------------
# HTML 雜訊
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ctx, expected", [
    (["body", "nav.main-menu", "ul", "li"], "html_noise:tag=nav"),
    (["body", "header.site-header", "div.logo"], "html_noise:site_header"),
    (["body", "div#cookie-banner.cookie-consent", "p"], "html_noise:attr=cookie-banner"),
    (["body", "main", "div.ad-slot.ad-top", "p"], "html_noise:attr=ad-slot"),
    (["body", "main", "article.post", "header.post-header", "h1"], None),  # 文章自己的 header 要保留
    (["body", "main", "article.post", "p"], None),
    (["body", "main", "article", "div.shadow-box", "p"], None),           # 'shadow' 不是 'ad'
])
def test_classify_html_noise_by_dom(clean_mod, ctx, expected):
    blk = make_block(0, "some text", page=None, html_context=ctx)
    assert clean_mod.classify_html_noise(blk) == expected


# 合成 DOM 路徑（SYNTHETIC）：仿 Charlotte Tilbury 文章頁，正文、步驟與商品輪播都包在 div.widget 裡
@pytest.mark.parametrize("widget_desc, expected", [
    # 正文型 widget → 保留
    ("div.widget.widget--blog_content_block.ctContainer", None),
    ("div.widget.widget--how_to_steps.ctContainer", None),
    # 商品輪播：修飾 class 不是正文 → 仍是雜訊
    ("div.widget.widget--product_listing.widget--is-themed.ctContainer", "html_noise:attr=widget"),
    # 修飾 class 本身含雜訊詞（related）優先於 post → 仍是雜訊
    ("div.widget.widget--related_posts", "html_noise:attr=widget"),
    # 沒有修飾 class 的 widget → 維持原本保守判斷
    ("div.widget", "html_noise:attr=widget"),
    # 正文型 widget 只豁免 widget 名稱，同元素其他雜訊 class 仍生效
    ("div.widget.widget--blog_content_block.newsletter", "html_noise:attr=newsletter"),
])
def test_content_widget_kept_other_widgets_removed(clean_mod, widget_desc, expected):
    ctx = ["body", "main.c-page__container", "div.BlogPost", widget_desc, "article.BlogContentBlock", "p"]
    blk = make_block(0, "Darlings, deep berry shades look sensational.", page=None, html_context=ctx)
    assert clean_mod.classify_html_noise(blk) == expected


@pytest.mark.parametrize("desc, expected", [
    # 駝峰命名拆字後才認得出 related（SYNTHETIC，仿 CSS Modules 產生的 class）
    ("section.RelatedContentCarousel_container__JGAc3", "html_noise:attr=RelatedContentCarousel_container__JGAc3"),
    # 拆字後不能誤傷：Shadow 不是 ad、BlogContentBlock 不含雜訊詞
    ("div.ShadowBox", None),
    ("article.BlogContentBlock", None),
    # 'shop' 為獨立單字時是購物模組（仿 L'Oréal 的 oap-shop-the-product）
    ("div#oap-shop-the-product-1234.oap-shop-the-product", "html_noise:attr=oap-shop-the-product-1234"),
    # 'workshop' 不是 shop 這個單字
    ("div.workshop-notes", None),
])
def test_camel_case_and_shop_attrs(clean_mod, desc, expected):
    blk = make_block(0, "some text", page=None, html_context=["body", "main", desc, "p"])
    assert clean_mod.classify_html_noise(blk) == expected


@pytest.mark.parametrize("text, block_type, expected", [
    ("Shop Cherry Talk", "paragraph", "html_noise:call_to_action"),
    ("Discover more red lipstick looks to try", "paragraph", "html_noise:call_to_action"),
    ("Discover how to apply lipstick", "list_item", "html_noise:call_to_action"),
    # 章節標題要保留
    ("Discover the Best Lipstick Colours for Dark Skin", "heading", None),
    # 完整句子（有句號）視為正文，不自動刪
    ("Discover a warm coral shade if your skin has golden undertones.", "paragraph", None),
    # 不是以導流動詞開頭
    ("Wear it with: Pillow Talk Medium Lip Cheat", "paragraph", None),
])
def test_call_to_action_lines(clean_mod, text, block_type, expected):
    extra = {"heading_level": 2} if block_type == "heading" else {}  # heading 區塊必須有層級
    blk = make_block(0, text, page=None, block_type=block_type,
                     html_context=["body", "main", "article", "p"], **extra)
    assert clean_mod.classify_html_noise(blk) == expected


def test_long_line_starting_with_discover_is_kept(clean_mod):
    text = "Discover " + "why deep berry shades balance a cool undertone and brighten the complexion " * 2
    blk = make_block(0, text, page=None, html_context=["body", "main", "article", "p"])
    assert clean_mod.classify_html_noise(blk) is None


def test_content_widget_inside_nav_still_noise(clean_mod):
    # 外層已是 nav 時，裡面即使是正文型 widget 也不救回
    ctx = ["body", "nav.menu", "div.widget.widget--blog_content_block", "p"]
    blk = make_block(0, "some text", page=None, html_context=ctx)
    assert clean_mod.classify_html_noise(blk) == "html_noise:tag=nav"


@pytest.mark.parametrize("text", ["Advertisement", "Read more »", "Share this article", "Accept all cookies",
                                  "© 2024 BeautyLab. All rights reserved."])
def test_boilerplate_text_is_noise(clean_mod, text):
    blk = make_block(0, text, page=None, html_context=["body", "main", "article", "p"])
    assert clean_mod.classify_html_noise(blk) == "html_noise:boilerplate_text"


def test_noise_region_with_colour_data_kept_for_review(clean_mod):
    blk = make_block(0, "小提醒：膚色 a* 值高於 15 的人適合偏藍調唇色。", page=None,
                     html_context=["body", "main", "article", "aside.tip-box", "p"])
    out, issues = clean_mod.clean_block(blk, "html", None)
    assert not out.is_removed and out.needs_review
    assert issues[0].issue_type == "noise_candidate_contains_data"


# ---------------------------------------------------------------------------
# 端到端：學術 PDF（TH013 前 12 頁）
# ---------------------------------------------------------------------------


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_pdf_raw_file_untouched(leeds_cleaned):
    assert leeds_cleaned.manifest.sha256 == _sha(LEEDS_PDF)
    assert leeds_cleaned.manifest.pages_processed == list(range(1, 13))


def test_pdf_keeps_raw_and_cleaned_text(leeds_extracted, leeds_cleaned):
    assert len(leeds_extracted.blocks) == len(leeds_cleaned.blocks)
    for e, c in zip(leeds_extracted.blocks, leeds_cleaned.blocks):
        assert (e.block_id, e.raw_text, e.page_number, e.block_type) == (c.block_id, c.raw_text, c.page_number, c.block_type)
        assert c.cleaned_text is not None


def test_pdf_page_number_footers_removed(leeds_cleaned):
    footers = [b for b in leeds_cleaned.blocks if b.block_type == "page_footer"]
    assert len(footers) == 12
    assert all(b.is_removed and b.removal_reason == "page_footer:page_number" for b in footers)


def test_pdf_no_content_lost(clean_mod, leeds_cleaned):
    for b in leeds_cleaned.blocks:
        if not b.is_removed:
            assert clean_mod.verify_content_preserved(b.raw_text, b.cleaned_text) == [], b.block_id
    text = " ".join(b.cleaned_text for b in leeds_cleaned.blocks if not b.is_removed)
    for fact in ["380 nm to 740 nm", "420 nm", "530 nm", "560 nm", "A, D65, and D50",
                 "Fifteen Chinese female participants", "controlled light booth equipped with D65 lighting"]:
        assert fact in text, fact


def test_pdf_paragraph_line_breaks_fixed(leeds_cleaned):
    paras = [b for b in leeds_cleaned.blocks if b.block_type == "paragraph" and not b.is_removed]
    assert paras and all("\n" not in b.cleaned_text for b in paras)
    assert all("  " not in b.cleaned_text for b in paras)


def test_pdf_sections_and_headings(leeds_cleaned):
    blocks = leeds_cleaned.blocks
    heading = next(b for b in blocks if b.block_type == "heading" and b.cleaned_text.startswith("2.1.2.1"))
    assert heading.heading_level == 4 and heading.page_number == 12
    assert heading.section.endswith("2.1 Colour Science > 2.1.2 CIE Colorimetry > 2.1.2.1 CIE Standard Illuminants")
    bg = next(b for b in blocks if b.cleaned_text.startswith("The application of cosmetics"))
    assert bg.section == "Chapter 1. Introduction > 1.1 Background" and bg.page_number == 7
    abstract = next(b for b in blocks if b.cleaned_text.startswith("This study examines"))
    assert abstract.section == "Abstract"
    assert any(b.block_type == "figure_caption" and b.cleaned_text.startswith("Figure 2.1") for b in blocks)


def test_pdf_cleaning_is_idempotent(clean_mod, leeds_extracted, leeds_cleaned):
    again = clean_mod.clean_document(leeds_cleaned)  # 把已清洗的再清一次
    assert [b.model_dump() for b in again.blocks] == [b.model_dump() for b in leeds_cleaned.blocks]


def test_pdf_json_roundtrip(tmp_path, leeds_cleaned):
    path = leeds_cleaned.save(tmp_path / "out.json")
    loaded = Document.load(path)
    assert loaded == leeds_cleaned
    assert json.loads(path.read_text(encoding="utf-8"))["stage"] == "cleaned"


# ---------------------------------------------------------------------------
# 端到端：專業彩妝 HTML 文章（合成 fixture）
# ---------------------------------------------------------------------------


def _kept_text(doc):
    return "\n".join(b.cleaned_text for b in doc.blocks if not b.is_removed)


def test_html_noise_removed(html_cleaned):
    kept = _kept_text(html_cleaned)
    for noise in ["本網站使用 Cookie", "首頁", "Advertisement", "限時 5 折", "分享到 Facebook",
                  "延伸閱讀", "訂閱電子報", "好實用", "All rights reserved", "Read more", "gtag"]:
        assert noise not in kept, noise
    removed = [b for b in html_cleaned.blocks if b.is_removed]
    assert removed and all(b.removal_reason.startswith("html_noise") for b in removed)
    assert all(b.raw_text for b in removed)  # 原文仍在


def test_html_article_content_kept(html_cleaned):
    kept = _kept_text(html_cleaned)
    for must in [
        "冷暖膚色怎麼挑唇膏？從 CIELAB 數據看唇色對膚色的影響",     # 標題（在 article 的 header 裡）
        "彩妝師 林小雨",                                             # &nbsp; 已轉空白
        "CIE L*a*b* 與 ΔE00 色差",                                   # <sub>00</sub> 串接
        "光源：D65 標準光源，觀察者角度 10°",                         # 研究條件
        "n = 30 名台灣女性，年齡 20–35 歲",
        "ΔE*ab = √[(ΔL*)² + (Δa*)² + (Δb*)²]",                       # 公式
        "ΔE*ab < 2.3",
        "未考慮室內暖光（約 3000 K）情境",                            # 研究限制
        "樣本僅限東亞女性",
    ]:
        assert must in kept, must


def test_html_cielab_table_preserved(html_cleaned):
    table = next(b for b in html_cleaned.blocks if b.block_type == "table" and "L*" in b.raw_text)
    assert table.cleaned_table_data[0] == ["色號", "L*", "a*", "b*", "C*ab", "h°"]
    assert table.table.column_names == ["色號", "L*", "a*", "b*", "C*ab", "h°"]
    assert table.table.row_labels == ["MLBB 玫瑰", "正紅", "珊瑚橘", "莓果紫"]
    assert table.table.caption.startswith("Table 1.") and table.table.units == {}
    assert ["莓果紫", "30.25", "35.48", "−3.12", "35.62", "355.0"] in table.cleaned_table_data
    assert table.section.endswith("唇膏色號的 CIELAB 數值")
    assert not table.is_removed


def test_html_unreadable_tables_flagged(html_cleaned):
    types = {i.issue_type for i in html_cleaned.issues}
    assert {"table_not_machine_readable", "table_needs_review", "noise_candidate_contains_data"} <= types
    by_id = {b.block_id: b for b in html_cleaned.blocks}
    img_caption = next(b for b in html_cleaned.blocks if b.block_type == "table_caption" and "Table 2" in b.raw_text)
    region = next(by_id[i] for i in img_caption.linked_block_ids)
    assert region.block_type == "image_region" and region.region_kind == "table_image"
    assert region.parse_status == "failed" and region.needs_review and not region.is_removed


def test_html_section_rebuilt_without_noise_headings(html_cleaned):
    for b in html_cleaned.blocks:
        if not b.is_removed and b.section:
            assert "延伸閱讀" not in b.section and "留言" not in b.section


def test_html_manifest(html_cleaned):
    m = html_cleaned.manifest
    assert m.source_type == "html" and m.sha256 == _sha(HTML_FIXTURE)
    assert m.source_url == "https://beautylab.example.com/articles/lipstick-cielab"
    assert m.metadata["author"] == "林小雨"
    assert 0.95 <= float(m.metadata["text_coverage"]) <= 1.05
    assert all(b.page_number is None and b.source_locator for b in html_cleaned.blocks)


# ---------------------------------------------------------------------------
# 文件層級的行尾斷字判斷
# ---------------------------------------------------------------------------


def _hyphen_doc(texts):
    return [make_block(i, t) for i, t in enumerate(texts)]


def test_hyphenation_uses_document_evidence(clean_mod):
    blocks = _hyphen_doc(["The skin colour was measured.", "A self-esteem scale was used.",
                          "lip col-\nour and self-\nesteem were rated"])
    pol = clean_mod.build_hyphenation_policy(blocks)
    out, ops = clean_mod.fix_pdf_line_breaks(blocks[2].raw_text, pol.decide)
    assert out == "lip colour and self-esteem were rated"       # colour 有證據 → 去掉；self-esteem 有證據 → 保留
    assert "dehyphenated" in ops and "joined_hyphenated_word_kept_hyphen" in ops


def test_word_document_keeps_unknown_compounds(clean_mod):
    """Word 論文（行尾斷字很少）：沒有證據時保留連字號，例如 Leeds 的 black-body。"""
    texts = [f"Line {i} of an ordinary paragraph without hyphenation." for i in range(60)] + ["a black-\nbody radiator"]
    pol = clean_mod.build_hyphenation_policy(_hyphen_doc(texts))
    assert not pol.typeset
    assert clean_mod.fix_pdf_line_breaks("a black-\nbody radiator", pol.decide)[0] == "a black-body radiator"


def test_typeset_journal_dehyphenates_unknown_words(clean_mod):
    """期刊排版（行尾斷字頻繁）：沒有證據時視為自動斷字，去掉連字號並標記為推測。"""
    texts = ["The lips have played a role in cosmetic tech-\nniques since the cen-\ntury began and their popular-\nity grew."]
    pol = clean_mod.build_hyphenation_policy(_hyphen_doc(texts))
    assert pol.typeset
    out, ops = clean_mod.fix_pdf_line_breaks(texts[0], pol.decide)
    assert out == "The lips have played a role in cosmetic techniques since the century began and their popularity grew."
    assert "dehyphenated_typeset_guess" in ops


def test_leeds_compounds_still_kept(leeds_full_cleaned):
    text = " ".join(b.cleaned_text for b in leeds_full_cleaned.blocks if not b.is_removed)
    for w in ["self-esteem", "black-body", "p-value", "self-concept"]:
        assert w in text, w
