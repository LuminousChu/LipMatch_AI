"""TX001 粗體引言與詞彙表修復（2026-10-17）：編號粗體引言被切成標題、詞彙表標題沒認出來、詞條併成一段。

對應問題（詳見 docs/mvp/handoffs/ETL-TX001.md 第八節第 1、2 點，與 ETL-TX001-runin.md）：
1. 「**1. Vein test:** Observe … under the ⏎ wrist. If …」：第一行 <= 60 字、有「1.」編號、行首粗體，
   被 _line_kind 判成標題；頁邊欄「Notes」又排在第一行與第二行之間，後半句變成小寫開頭的孤立段落。
2. 「Glossary」首字放大但沒置中，不算標題；底下「**詞條:** 定義」一行接一行，被併成一大段。

反例同樣重要：結構式摘要（Aim: / Methods:）、作者貢獻等不在詞彙表區的粗體引言不拆；
只有編號粗體、沒有冒號引言的小標仍是標題；內文字級、未置中的「Glossary」字樣不是標題。
合成資料標示 SYNTHETIC（tests/fixtures/synthetic_runin_pdf.py），不使用真實全文。
"""
from __future__ import annotations

import pymupdf
import pytest

from tests.fixtures.synthetic_runin_pdf import (ABSTRACT_LINES, AFTER_GLOSSARY_HEADING, GLOSSARY_CONTINUED,
                                                 GLOSSARY_ENTRIES, STEP_ITEMS)

R = pymupdf.Rect


@pytest.fixture(scope="module")
def runin_doc(extract_mod, tmp_path_factory):
    """四頁合成 PDF 的擷取結果（SYNTHETIC）。"""
    from tests.fixtures.synthetic_runin_pdf import build_runin_pdf
    pdf = build_runin_pdf(tmp_path_factory.mktemp("tx001_runin") / "synthetic_runin.pdf")
    return extract_mod.extract_pdf(pdf, "SYN_RUNIN")


def _blocks(doc, page: int, block_type: str) -> list:
    """取出某頁某型別的區塊。"""
    return [b for b in doc.blocks if b.page_number == page and b.block_type == block_type]


def _span(text: str, bold: bool, x0: float = 0.0) -> dict:
    """get_text("dict") 格式的 span；只填 is_run_in_label 會用到的欄位。"""
    return {"text": text, "flags": 16 if bold else 0, "font": "Helvetica-Bold" if bold else "Helvetica",
            "size": 10.0, "bbox": (x0, 0, x0 + 6 * len(text), 12)}


def _line(extract_mod, text: str, x0: float, y0: float, x1: float, *, size: float = 10.0,
          bold: bool = False, run_in: bool = False, height: float = 12.0):
    """建立一條 Line；只填 group_lines 會用到的欄位。"""
    return extract_mod.Line(text=text, bbox=R(x0, y0, x1, y0 + height), size=size, bold_all=bold,
                            first_bold=bold or run_in, run_in_label=run_in)


# ---------------------------------------------------------------------------
# 整份合成 PDF：修好的
# ---------------------------------------------------------------------------

def test_numbered_run_in_is_one_list_item(runin_doc):
    """編號粗體引言不是標題：引言與後半句併成同一個條列項目，旁邊的 Notes 不切斷它。"""
    items = _blocks(runin_doc, 1, "list_item")
    assert len(items) == len(STEP_ITEMS)
    for item, (bold, first, rest) in zip(items, STEP_ITEMS):
        assert item.raw_text.startswith(bold + first)
        for t in rest:
            assert t in item.raw_text
    headings = [b.raw_text for b in _blocks(runin_doc, 1, "heading")]
    assert headings == ["Notes"]                       # 只有頁邊欄標籤是標題
    assert not _blocks(runin_doc, 1, "paragraph")      # 沒有孤立的後半句


def test_run_in_item_keeps_section_before_side_label(runin_doc):
    """側欄標籤順延到條列之後：第一個條列的章節路徑不被「Notes」取代。"""
    first = _blocks(runin_doc, 1, "list_item")[0]
    assert first.section is None or "Notes" not in first.section
    assert not any("Wrist check" in (b.section or "") for b in runin_doc.blocks)


def test_glossary_title_is_heading(runin_doc):
    """首字放大、未置中的 Glossary 是標題（level 1），不是單字段落。"""
    headings = _blocks(runin_doc, 3, "heading")
    assert [b.raw_text for b in headings] == ["Glossary"]
    assert headings[0].heading_level == 1
    assert not any(b.raw_text.strip() == "Glossary" for b in _blocks(runin_doc, 3, "paragraph"))


def test_glossary_entries_are_separate_paragraphs(runin_doc):
    """每個詞條一段：兩行的詞條保持完整，不和下一個詞條併在一起；章節是 Glossary。"""
    paras = _blocks(runin_doc, 3, "paragraph")
    assert len(paras) == len(GLOSSARY_ENTRIES)
    for p, (bold, first, rest) in zip(paras, GLOSSARY_ENTRIES):
        assert p.raw_text.splitlines() == [bold + first] + rest
        assert p.section == "Glossary"


def test_glossary_continues_on_next_page(runin_doc):
    """跨頁詞彙表：續頁開頭沒有標題，詞條仍各自成段；之後的標題結束詞彙表區，其下的粗體引言不拆。"""
    paras = _blocks(runin_doc, 4, "paragraph")
    for p, (bold, first, rest) in zip(paras, GLOSSARY_CONTINUED):
        assert p.raw_text.splitlines() == [bold + first] + rest
    assert AFTER_GLOSSARY_HEADING in [b.raw_text for b in _blocks(runin_doc, 4, "heading")]
    abstract = [p for p in paras if p.raw_text.startswith("Aim:")]
    assert len(abstract) == 1 and len(abstract[0].raw_text.splitlines()) == len(ABSTRACT_LINES)


def test_glossary_entries_not_flagged_as_merged(runin_doc, clean_mod):
    """清洗後：詞條已分開，不再觸發「詞彙表多條併段」待確認。"""
    cleaned = clean_mod.clean_document(runin_doc)
    flagged = [b for b in cleaned.blocks if "flagged_merged_glossary_entries" in b.cleaning_ops]
    assert not flagged


# ---------------------------------------------------------------------------
# 整份合成 PDF：反例（不能因此改變）
# ---------------------------------------------------------------------------

def test_structured_abstract_not_split_outside_glossary(runin_doc):
    """詞彙表區以外的粗體引言（結構式摘要 Aim: / Methods: / Results:）維持同一段。"""
    abstract = [b for b in _blocks(runin_doc, 2, "paragraph") if b.raw_text.startswith("Aim:")]
    assert len(abstract) == 1
    assert abstract[0].raw_text.splitlines() == [bold + regular for bold, regular in ABSTRACT_LINES]


def test_plain_glossary_word_is_not_heading(runin_doc):
    """內文字級、未置中、沒有放大字母的「Glossary」字樣不是標題。"""
    assert any(b.raw_text.strip() == "Glossary" for b in _blocks(runin_doc, 2, "paragraph"))
    assert "Glossary" not in [b.raw_text for b in _blocks(runin_doc, 2, "heading")]


def test_number_only_bold_heading_still_heading(runin_doc):
    """只有編號是粗體、沒有冒號引言的小標（「**2.** Colour basics」）仍是標題。"""
    assert "2. Colour basics" in [b.raw_text for b in _blocks(runin_doc, 2, "heading")]


# ---------------------------------------------------------------------------
# 單元：is_run_in_label
# ---------------------------------------------------------------------------

def test_run_in_label_positive(extract_mod):
    """粗體引言以冒號結尾、後面接非粗體文字 → 是引言。"""
    assert extract_mod.is_run_in_label([_span("1.", True), _span("Wrist check:", True), _span(" Look at", False)])
    assert extract_mod.is_run_in_label([_span("Blusher:", True), _span(" Powder or cream", False)])


@pytest.mark.parametrize("spans", [
    [_span("Wrist check", True), _span(" look at the lines", False)],             # 沒有冒號
    [_span("Wrist check:", True), _span(" Look at", True)],                       # 整行粗體（標題／標籤）
    [_span("Wrist check:", True)],                                                # 只有引言，沒有正文
    [_span("Table 1:", True), _span(" 0.52 0.61", False)],                        # 後面只有數字
    [_span("Note:", False), _span(" plain text", False)],                         # 引言不是粗體
    [_span("A" * 61 + ":", True), _span(" text", False)],                         # 粗體太長，不是引言
    [_span("1.", False), _span("Wrist check:", True), _span(" Look", False)],     # 行首不是粗體
])
def test_run_in_label_negative(extract_mod, spans):
    """不符合「行首粗體引言＋冒號＋非粗體正文」的版式 → 不是引言。"""
    assert not extract_mod.is_run_in_label(spans)


# ---------------------------------------------------------------------------
# 單元：_line_kind／group_lines
# ---------------------------------------------------------------------------

def test_line_kind_numbered_run_in_is_list_item(extract_mod):
    """「1. Wrist check: Look at …」（<= 60 字、行首粗體）是條列，不是標題；同一行沒有引言時仍是標題。"""
    run_in = _line(extract_mod, "1. Wrist check: Look at the lines", 120, 100, 400, run_in=True)
    plain = _line(extract_mod, "1. Wrist check: Look at the lines", 120, 100, 400)
    plain.first_bold = True
    assert extract_mod._line_kind(run_in, 10.0, False, 298) == "list_item"
    assert extract_mod._line_kind(plain, 10.0, False, 298) == "heading"


def test_glossary_carries_to_next_page_until_heading(extract_mod):
    """跨頁詞彙表：in_glossary=True 時頁首的詞條各自成段；遇到其他標題後不再拆。"""
    lines = [
        _line(extract_mod, "Blusher: Powder or cream", 120, 100, 400, run_in=True),
        _line(extract_mod, "Gloss: A shiny finish", 120, 114, 400, run_in=True),
        _line(extract_mod, "Answer Key", 120, 150, 250, size=14, bold=True),
        _line(extract_mod, "Aim: To compare", 120, 180, 400, run_in=True),
        _line(extract_mod, "Methods: Ten volunteers", 120, 194, 400, run_in=True),
    ]
    kinds = ["text", "text", "heading", "text", "text"]
    carried = extract_mod.group_lines(lines, kinds, 10.0, in_glossary=True)
    assert [len(gl) for _, gl in carried] == [1, 1, 1, 2]
    fresh = extract_mod.group_lines(lines, kinds, 10.0)
    assert [len(gl) for _, gl in fresh] == [2, 1, 2]


def test_side_label_not_beside_does_not_merge(extract_mod):
    """標籤在正文上方（不是同高並排）時，下一行不跨過它接回前一段。"""
    lines = [
        _line(extract_mod, "1. Wrist check: Look at the lines", 120, 100, 400),
        _line(extract_mod, "Notes", 20, 116, 60, size=12, bold=True),
        _line(extract_mod, "inner arm in daylight", 138, 114, 400),
    ]
    kinds = ["list_item", "heading", "text"]
    assert not extract_mod.continues_beside_label([("list_item", [lines[0]]), ("heading", [lines[1]])],
                                                  lines[2], "text")
    assert [k for k, _ in extract_mod.group_lines(lines, kinds, 10.0)] == ["list_item", "heading", "text"]


def test_side_label_overlapping_text_does_not_merge(extract_mod):
    """「標籤」和正文左右重疊（是正文上方的小標，不是側欄）→ 不跨過。"""
    body = _line(extract_mod, "first line of a paragraph", 120, 100, 400)
    label = _line(extract_mod, "Procedure", 120, 101, 200, bold=True)
    nxt = _line(extract_mod, "second line", 120, 114, 400)
    assert not extract_mod.continues_beside_label([("text", [body]), ("label", [label])], nxt, "text")


def test_side_label_far_gap_does_not_merge(extract_mod):
    """側欄標籤同高並排，但下一行離前一段太遠（新段落）→ 不接回。"""
    body = _line(extract_mod, "1. Wrist check: Look at the lines", 120, 100, 400)
    side = _line(extract_mod, "Notes", 20, 101, 60, size=12, bold=True)
    far = _line(extract_mod, "a new paragraph much lower", 120, 160, 400)
    assert not extract_mod.continues_beside_label([("list_item", [body]), ("heading", [side])], far, "text")
