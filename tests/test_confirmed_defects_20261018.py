"""2026-10-18 核心證據抽查確認的三類清洗缺陷（SYNTHETIC 單元測試，不讀真實 PDF）。

1. TX001 p.17：左下圖說與右欄正文同高被併成一行，正文句尾跑進圖說，段落再跨過未解析表格接到 p.18 的半句。
2. AR003 p.8：書末章名（Funding、References…）與正文同字級，沒被認成標題，參考書目留在 Discussion 被匯出。
3. AR003 p.4：SAGE 的 Advent 數學字型把「=」存成「¼」、「η」存成「Z」。
詳見 docs/mvp/handoffs/2026-10-18_TX001與AR003已確認清洗缺陷修復.md。
"""
from __future__ import annotations

import pymupdf
import pytest

from lipmatch.eligibility import EXCLUDED_SECTION_RE
from schemas import TextBlock


# 建立一條合成的視覺行（只填測試用得到的欄位）。
def _line(extract_mod, text: str, bbox: tuple, size: float = 10.0, fonts: set | None = None):
    return extract_mod.Line(text=text, bbox=pymupdf.Rect(*bbox), size=size, bold_all=False, first_bold=False,
                            fonts=fonts or {"Body"})


# 建立一個合成的文字區塊。
def _block(bid: str, btype: str, order: int, page: int, text: str, **kw) -> TextBlock:
    if btype == "table":
        kw.setdefault("table_data", [["a", "b"]])   # 表格區塊必須有儲存格
    return TextBlock(doc_id="SYN", block_id=bid, block_type=btype, order=order, page_number=page,
                     raw_text=text, cleaned_text=text, **kw)


# ---------------------------------------------------------------------------
# 3. Advent 數學字型
# ---------------------------------------------------------------------------
def test_advent_symbol_font_maps_equals_times_eta(extract_mod):
    assert extract_mod.map_symbol_font(" ¼", "AdvMT_SY") == " ="
    assert extract_mod.map_symbol_font(" \x02", "AdvMT_SY") == " ×"
    assert extract_mod.map_symbol_font("Z", "AdvPi1") == "η"
    assert extract_mod.map_symbol_font("Z", "ABCDEF+AdvPi1") == "η"     # 子集前綴


def test_other_fonts_keep_quarter_and_z(extract_mod):
    # 反例：一般字型的 ¼ 與 Z 是真的字，不能動
    assert extract_mod.map_symbol_font("¼ cup", "AdvTimes") == "¼ cup"
    assert extract_mod.map_symbol_font("Zinc", "Times-Roman") == "Zinc"


def test_partial_eta_squared_pattern(extract_mod):
    assert extract_mod.PARTIAL_ETA_SQUARED_RE.sub("ηp²", "p < .001, ηp2= .44") == "p < .001, ηp²= .44"
    assert extract_mod.PARTIAL_ETA_SQUARED_RE.sub("ηp²", "ηp20") == "ηp20"   # 後面還有數字就不是上標 2


# ---------------------------------------------------------------------------
# 1. TX001 p.17 圖說與右欄正文
# ---------------------------------------------------------------------------
IMAGE = pymupdf.Rect(61, 383, 199, 454)          # p.17 的圖
CAPTION = pymupdf.Rect(68, 456, 194, 469)        # 「Figure 1.2: Cream foundation」
BODY = pymupdf.Rect(211, 455, 470, 472)          # 右欄「brand of foundation to go well with make-up.」


def test_caption_under_side_image_detected(extract_mod):
    assert extract_mod._caption_under_side_image(CAPTION, BODY, [IMAGE])


def test_caption_split_needs_left_caption_under_image(extract_mod):
    # 反例：沒有圖、圖說在右邊、或圖說不在圖正下方 → 不拆（TX001 p.38、p.88 一律拆會產生新錯接）
    assert not extract_mod._caption_under_side_image(CAPTION, BODY, [])
    assert not extract_mod._caption_under_side_image(BODY, CAPTION, [IMAGE])
    far = pymupdf.Rect(61, 300, 199, 380)
    assert not extract_mod._caption_under_side_image(CAPTION, BODY, [far])


def test_caption_body_requires_text_after_label(extract_mod):
    assert extract_mod._has_caption_body("Figure 1.2: Cream foundation")
    assert not extract_mod._has_caption_body("FIGURE 1")      # AR001：標籤與說明分成兩個單位，照舊合併


def test_caption_continuation_must_start_within_caption_width(extract_mod):
    cap = _line(extract_mod, "Figure 1.2: Cream foundation", tuple(CAPTION), size=9.0)
    right = _line(extract_mod, "(i) Liquid (cream) foundation: Cream foundation is", (223, 470, 538, 487), size=9.0)
    under = _line(extract_mod, "and its uses", (80, 470, 180, 483), size=9.0)
    blocks = extract_mod.group_lines([cap, right], ["figure_caption", "text"], 10.0)
    assert [k for k, _ in blocks] == ["figure_caption", "text"]          # 右欄正文不是圖說續行
    blocks = extract_mod.group_lines([cap, under], ["figure_caption", "text"], 10.0)
    assert [k for k, _ in blocks] == ["figure_caption"]                  # 圖說寬度內的縮排續行照舊合併


def test_numbered_item_lookahead_skips_beside_caption(extract_mod):
    # TX001 p.59：「1. Check the machine … risks」旁邊是兩行圖說，續行「… ensure safety.」在後面
    item = _line(extract_mod, "1. Check the machine for damage, faults, and risks", (227, 577, 538, 595), 12.0)
    lines = [item,
             _line(extract_mod, "Figure 2.5: Different types of", (66, 579, 189, 592), 9.0),
             _line(extract_mod, "electric brushes", (94, 590, 162, 603), 9.0),
             _line(extract_mod, "before operating; if detected, take the necessary", (245, 593, 538, 610), 12.0),
             _line(extract_mod, "action as per organisation-approved procedures", (245, 609, 538, 626), 12.0),
             _line(extract_mod, "and ensure safety.", (245, 625, 348, 642), 12.0)]
    assert extract_mod._continuation_ends_sentence(lines, 0)


def test_numbered_item_lookahead_still_stops_at_unrelated_side_text(extract_mod):
    # 反例：旁邊不是圖說的文字仍會切斷往下看（行為與修改前相同）
    item = _line(extract_mod, "2.1 Colour science basics", (227, 577, 538, 595), 12.0)
    lines = [item, _line(extract_mod, "sidebar note", (66, 579, 189, 592), 12.0),
             _line(extract_mod, "ends here.", (245, 593, 538, 610), 12.0)]
    assert not extract_mod._continuation_ends_sentence(lines, 0)


# ---------------------------------------------------------------------------
# 2. AR003 書末章名
# ---------------------------------------------------------------------------
def test_back_matter_heading_in_heading_font_promoted(extract_mod):
    head = _line(extract_mod, "References", (52, 359, 104, 369), fonts={"AdvP7D0F"})
    body = _line(extract_mod, "Adelson, E. H. (2000). Lightness perception …", (52, 375, 446, 384), 9.0, {"AdvTimes"})
    assert extract_mod.promote_back_matter_headings([head, body], ["text", "text"]) == ["heading", "text"]
    fund = _line(extract_mod, "Funding", (52, 310, 90, 320), fonts={"AdvP7D0F"})
    assert extract_mod.promote_back_matter_headings([fund, body], ["label", "text"])[0] == "heading"


def test_back_matter_word_in_body_font_not_promoted(extract_mod):
    # 反例：和下一行同字型（正文裡剛好一行是「Funding」）、或不是單獨一行的章名
    a = _line(extract_mod, "Funding", (52, 310, 90, 320), fonts={"AdvTimes"})
    body = _line(extract_mod, "was provided by the lab.", (52, 322, 446, 332), fonts={"AdvTimes"})
    assert extract_mod.promote_back_matter_headings([a, body], ["text", "text"]) == ["text", "text"]
    b = _line(extract_mod, "References were checked twice", (52, 310, 200, 320), fonts={"AdvP7D0F"})
    assert extract_mod.promote_back_matter_headings([b, body], ["text", "text"]) == ["text", "text"]


@pytest.mark.parametrize("section", ["Funding", "Author Biographies", "Conflict of interest", "Conflicts of Interest",
                                     "Declaration of Conflicting Interests", "References"])
def test_back_matter_sections_excluded_from_rag(section):
    assert EXCLUDED_SECTION_RE.match(section)


@pytest.mark.parametrize("section", ["Discussion", "Results", "Introduction", "Methods"])
def test_research_sections_not_excluded(section):
    assert not EXCLUDED_SECTION_RE.match(section)


# ---------------------------------------------------------------------------
# 1b. 未解析表格後面的後半句：隔離，不自動接回
# ---------------------------------------------------------------------------
def test_lowercase_fragment_after_partial_table_isolated(clean_mod):
    blocks = [
        _block("SYN_p0017_b001", "paragraph", 0, 17, "(iii) Oil-based foundation: This also acts as the moisturiser."),
        _block("SYN_p0018_b001", "table", 1, 18, "| Types of Foundation | …", parse_status="partial",
               failure_reason="表格範圍內僅 92% 的文字落入儲存格", needs_review=True),
        _block("SYN_p0018_b002", "paragraph", 2, 18, "that catch light, making the face look brighter and more radiant."),
    ]
    clean_mod.flag_truncated_fragments(blocks)
    frag = blocks[2]
    assert frag.needs_review and any("未完整解析的表格 SYN_p0018_b001" in r for r in frag.review_reasons)


def test_fragment_after_ok_table_or_uppercase_not_isolated(clean_mod):
    # 反例：表格解析正常，或這段是大寫開頭的完整句 → 不標記
    ok_table = [
        _block("SYN_p0018_b001", "table", 0, 18, "| a | b |"),
        _block("SYN_p0018_b002", "paragraph", 1, 18, "that follows a parsed table."),
    ]
    clean_mod.flag_truncated_fragments(ok_table)
    assert not ok_table[1].needs_review
    upper = [
        _block("SYN_p0018_b001", "table", 0, 18, "| a |", parse_status="partial", failure_reason="x", needs_review=True),
        _block("SYN_p0018_b002", "paragraph", 1, 18, "Shimmer foundation adds a soft glow."),
    ]
    clean_mod.flag_truncated_fragments(upper)
    assert not upper[1].needs_review
