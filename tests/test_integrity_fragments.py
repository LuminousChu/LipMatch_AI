"""資料完整性修復第四批（2026-10-17）：殘缺段落與未解析內容的批次偵測，以及造成殘缺的共通版型。

對應問題：
- AR004：頁面較小，書眉與頁碼在上緣 7% 以外，被當成正文；第 11 頁正文被接在書眉後面。
- AR003／AR004：句號式圖表標題「Figure 3.　…」「Table 3. …」、標題續行、表格註記「Note. …」被當成正文，
  插在跨頁句子中間，正文接不起來或接到表格註記上。
- AR001／AR002：並排式或標籤在上的圖說「FIGURE 2」＋說明文字被當成正文，正文被接到圖說上。
- AR012／AR004 第 1 頁：授權聲明、基金註腳插在跨頁句子中間，句子接不起來。
- 其餘接不起來的段落：以 05 實際匯出的一筆紀錄為單位偵測，只剩半句的整筆標為待確認，不匯出。

每條規則都有反例（原文本來就不加句號的條列、正下方緊接下一段、正文提到 Figure、只出現兩頁的文字等）。
合成資料標示 SYNTHETIC；真實 PDF 測試在檔案不存在時略過。
"""
from __future__ import annotations

import json

import pytest

from lipmatch.eligibility import classify_blocks
from lipmatch.sources import SourceInfo
from schemas import Document, SourceManifest, TextBlock
from tests.conftest import ROOT, _load

SRC = SourceInfo(title="SYNTHETIC", authors=["Tester"], author_citation="Tester", year=2026, verified_from="synthetic")
AR004_PDF = next(iter(sorted((ROOT / "data" / "raw").rglob("AR004_*.pdf"))), None)
AR002_PDF = next(iter(sorted((ROOT / "data" / "raw").rglob("AR002_*.pdf"))), None)
AR012_PDF = next(iter(sorted((ROOT / "data" / "raw").rglob("AR012_*.pdf"))), None)


@pytest.fixture(scope="module")
def export_mod():
    return _load("lipmatch_export_fragments", "05_export_rag_ready.py")


def _blk(i, text, page=2, bbox=(48, 100, 250, 200), bt="paragraph", **kw):
    if bt == "heading":
        kw.setdefault("heading_level", 2)
    return TextBlock(doc_id="t", block_id=f"t_b{i:03d}", block_type=bt, order=i, page_number=page, bbox=bbox,
                     section="2 SYNTHETIC Methods", raw_text=text, cleaned_text=text, search_text=text, **kw)


def _pdf_doc(blocks):
    m = SourceManifest(doc_id="t", source_path="x.pdf", source_type="pdf", sha256="0" * 64,
                       file_size_bytes=0, extractor="test")
    return Document(stage="extracted", manifest=m, blocks=blocks)


# ---------------------------------------------------------------------------
# 上下緣 7% 以外的書眉與頁碼
# ---------------------------------------------------------------------------

def _page_set(page, header_y=53.0, header="SYNTHETIC Journal of Science Vol. 36, No. 1"):
    return [_blk(page * 10, str(page + 4), page=page, bbox=(48, header_y, 56, header_y + 11)),
            _blk(page * 10 + 1, header, page=page, bbox=(163, header_y, 353, header_y + 11)),
            _blk(page * 10 + 2, f"SYNTHETIC body text on page {page} that is long and different.", page=page,
                 bbox=(48, 80, 250, 600))]


def test_running_head_outside_margin_removed(clean_mod):
    blocks = sum((_page_set(p) for p in range(2, 6)), [])
    removed = clean_mod.detect_running_heads_outside_margin(blocks, 3)
    assert {b.block_id for b in blocks if b.bbox[1] == 53.0} == set(removed)
    assert all("outside_margin" in r for r in removed.values())


def test_running_head_on_two_pages_kept(clean_mod):
    blocks = sum((_page_set(p) for p in range(2, 4)), [])
    assert clean_mod.detect_running_heads_outside_margin(blocks, 3) == {}


def test_same_text_at_different_heights_kept(clean_mod):
    """同一句話在各頁出現、但高度不同（例如每頁開頭的固定句型）→ 不是書眉。"""
    blocks = sum((_page_set(p, header_y=53.0 + 20 * p) for p in range(2, 6)), [])
    removed = clean_mod.detect_running_heads_outside_margin(blocks, 3)
    assert not any("Journal" in b.raw_text for b in blocks if b.block_id in removed)


# ---------------------------------------------------------------------------
# 圖表標題變體與表格註記
# ---------------------------------------------------------------------------

def test_period_caption_and_tail(clean_mod):
    blocks = [_blk(0, "Table 3. SYNTHETIC relationships between measures: coefficients of correlation",
                   bbox=(48, 304, 470, 325)),
              _blk(1, "and F-values in parenthesis.", bbox=(48, 324, 137, 335)),
              _blk(2, "Note. D.F. are 1 and 18 for the SYNTHETIC coefficients against the redness and the",
                   bbox=(48, 373, 470, 386)),
              _blk(3, "perceptual yellowness: **p<0.01", bbox=(56, 384, 207, 396)),
              _blk(4, "SYNTHETIC body paragraph starts here and is long enough.", bbox=(48, 408, 250, 671))]
    clean_mod.mark_caption_variants(blocks)
    assert [b.block_type for b in blocks[:2]] == ["table_caption", "table_caption"]
    assert clean_mod.TABLE_NOTE_REASON in blocks[2].review_reasons
    assert clean_mod.TABLE_NOTE_REASON in blocks[3].review_reasons
    assert blocks[4].block_type == "paragraph" and not blocks[4].needs_review


def test_uppercase_body_right_below_caption_kept(clean_mod):
    """標題正下方緊貼、但以大寫開頭的是正文，不是標題續行。"""
    blocks = [_blk(0, "Table 3. SYNTHETIC relationships between measures.", bbox=(48, 304, 470, 316)),
              _blk(1, "Results of the SYNTHETIC analysis are reported in the following section.",
                   bbox=(48, 318, 470, 400))]
    clean_mod.mark_caption_variants(blocks)
    assert blocks[1].block_type == "paragraph"


def test_label_caption_beside_and_below(clean_mod):
    beside = [_blk(0, "FIGURE 6", bbox=(46, 240, 88, 248)),
              _blk(1, "Regions of interest defined on SYNTHETIC lips.", bbox=(98, 240, 536, 259))]
    below = [_blk(2, "FIGURE 2", page=3, bbox=(62, 244, 86, 250)),
             _blk(3, "Display of the SYNTHETIC lipstick colors.", page=3, bbox=(62, 251, 522, 259)),
             _blk(4, "and C0-C4 are SYNTHETIC red.", page=3, bbox=(62, 260, 147, 268))]
    clean_mod.mark_caption_variants(beside + below)
    assert all(b.block_type == "figure_caption" for b in beside + below)


@pytest.mark.parametrize("text", [
    "Figure 2 shows the SYNTHETIC results for each group.",      # 正文提到圖：沒有句號
    "Table 4.1, the hue angle is SYNTHETIC significant.",        # 句中引用表格
    "Figure 3. " + "SYNTHETIC very long caption text " * 30,     # 超過 600 字：不改型別
])
def test_body_mentions_not_captions(clean_mod, text):
    b = _blk(0, text)
    clean_mod.mark_caption_variants([b])
    assert b.block_type == "paragraph"


def test_note_tail_requires_tight_lowercase(clean_mod):
    note = _blk(0, "Note. SYNTHETIC values are means.", bbox=(48, 373, 470, 386))
    body = _blk(1, "Results show a SYNTHETIC trend that is long enough.", bbox=(48, 400, 250, 600))
    clean_mod.mark_caption_variants([note, body])
    assert not body.needs_review


def test_body_between_caption_and_note_flagged(clean_mod):
    """清洗時才認出的表格標題與表格註記之間的表頭文字是表身，不是正文。"""
    blocks = [_blk(0, "Table 3. SYNTHETIC relationships.", bbox=(48, 304, 470, 316)),
              _blk(1, "vs Perceptual redness vs Perceptual yellowness", bbox=(276, 342, 454, 352)),
              _blk(2, "Note. SYNTHETIC D.F. are 1 and 18.", bbox=(48, 373, 470, 386))]
    clean_mod.mark_caption_variants(blocks)
    assert blocks[1].needs_review and clean_mod.TABLE_FRAGMENT_REASONS[0] in blocks[1].review_reasons


# ---------------------------------------------------------------------------
# 段落接續略過行政資訊
# ---------------------------------------------------------------------------

def test_link_skips_boilerplate_footnote(clean_mod):
    blocks = [_blk(0, "SYNTHETIC body text ends with standardized measurements for its material", page=1,
                   bbox=(45, 400, 291, 730)),
              _blk(1, "Correspondence: SYNTHETIC Author (author@example.org)", page=1, bbox=(45, 735, 291, 750)),
              _blk(2, "characteristics, and SYNTHETIC continuation text.", page=2, bbox=(45, 30, 291, 147))]
    clean_mod.link_cross_page_continuations(blocks)
    assert blocks[2].continues_from_block_id == "t_b000"


# ---------------------------------------------------------------------------
# 只剩半句的匯出紀錄
# ---------------------------------------------------------------------------

def _flag(clean_mod, blocks):
    clean_mod.link_cross_page_continuations(blocks)
    clean_mod.flag_unfinished_chains(blocks)
    return blocks


def test_open_end_at_column_bottom_flagged(clean_mod):
    blocks = _flag(clean_mod, [_blk(0, "SYNTHETIC analysis used the correction if the Sphericity", bbox=(305, 691, 548, 775)),
                               _blk(1, "FIGURE 2", bt="figure_caption", page=3, bbox=(62, 244, 86, 250))])
    assert clean_mod.UNFINISHED_END_MSG in blocks[0].review_reasons


def test_open_end_mid_column_not_flagged(clean_mod):
    """下一段就在正下方：沒有句號是原文寫法（TX001 條列），不是被切斷。"""
    blocks = _flag(clean_mod, [_blk(0, "Eyeliner defines the SYNTHETIC eye area to help accentuate the lashes",
                                    bbox=(48, 100, 250, 140)),
                               _blk(1, "Mascara adds SYNTHETIC volume to the lashes.", bbox=(48, 150, 250, 600))])
    assert not blocks[0].needs_review


@pytest.mark.parametrize("ending, flagged", [("accentuate the eye and lashes", False), ("the oily centre panel or", True)])
def test_open_end_before_figure_needs_connector(clean_mod, ending, flagged):
    blocks = _flag(clean_mod, [_blk(0, f"SYNTHETIC instruction text that continues to {ending}", bbox=(48, 100, 250, 140)),
                               _blk(1, "", bt="image_region", bbox=(48, 150, 250, 300), parse_status="failed",
                                    needs_review=True, failure_reason="img", review_reasons=["img"]),
                               _blk(2, "SYNTHETIC later paragraph text.", bbox=(48, 310, 250, 600))])
    assert (clean_mod.UNFINISHED_END_MSG in blocks[0].review_reasons) is flagged


def test_lowercase_start_without_link_flagged(clean_mod):
    blocks = _flag(clean_mod, [_blk(0, "SYNTHETIC heading", bt="heading", bbox=(48, 50, 250, 60)),
                               _blk(1, "assumption was violated via the SYNTHETIC test.", bbox=(48, 70, 250, 300))])
    assert clean_mod.UNFINISHED_START_MSG in blocks[1].review_reasons


def test_colon_lead_before_unexported_formula(clean_mod):
    blocks = _flag(clean_mod, [_blk(0, "Each SYNTHETIC tristimulus is calculated using the following formulae:",
                                    bbox=(48, 100, 250, 140)),
                               _blk(1, "X = k ∫ S(λ) x(λ) dλ", bt="formula", bbox=(48, 150, 250, 170)),
                               _blk(2, "SYNTHETIC next paragraph.", bbox=(48, 180, 250, 600))])
    assert clean_mod.UNFINISHED_LEAD_MSG in blocks[0].review_reasons


def test_colon_lead_before_exported_list_kept(clean_mod):
    blocks = _flag(clean_mod, [_blk(0, "Here is a SYNTHETIC rundown of hues for different undertones:", bbox=(48, 100, 250, 140)),
                               _blk(1, "Cool skin: soft pinks and SYNTHETIC mauves.", bbox=(48, 150, 250, 600))])
    assert not blocks[0].needs_review


def test_html_not_checked(clean_mod):
    """網頁不套用：原文條列常不加句號（使用者決定保留的 PR020「Wear it with」）。"""
    m = SourceManifest(doc_id="h", source_path="x.html", source_type="html", sha256="0" * 64,
                       file_size_bytes=0, extractor="test")
    b = TextBlock(doc_id="h", block_id="h_b000", block_type="paragraph", order=0, source_locator="body[1]>p[1]",
                  raw_text="Wear it with: SYNTHETIC Lip Cheat in Medium and a long enough tail of words")
    cleaned = clean_mod.clean_document(Document(stage="extracted", manifest=m, blocks=[b]))
    assert not cleaned.blocks[0].needs_review


def test_eligibility_excludes_flagged_chain(clean_mod, export_mod, tmp_path):
    """標記不是只寫在報告：整筆被標記的紀錄確實不匯出，也不留下懸空續段。"""
    doc = clean_mod.clean_document(_pdf_doc([
        _blk(0, "SYNTHETIC first half of a sentence that stops at the bottom of the", bbox=(305, 400, 548, 775)),
        _blk(1, "3 SYNTHETIC Results", bt="heading", page=3, bbox=(48, 50, 250, 62)),   # 新章節：後半句沒有被擷取到
        _blk(2, "SYNTHETIC complete paragraph on the next page.", page=3, bbox=(48, 70, 250, 600)),
    ]))
    path, _ = export_mod.export(doc, SRC, tmp_path)
    records = [json.loads(s) for s in path.read_text(encoding="utf-8").splitlines()]
    assert all("first half" not in r["display_text"] for r in records)
    assert not export_mod.dangling_continuations(records)


# ---------------------------------------------------------------------------
# 真實文件
# ---------------------------------------------------------------------------

def _records(extract_mod, clean_mod, export_mod, pdf, doc_id, tmp_path_factory):
    doc = clean_mod.clean_document(extract_mod.extract_pdf(pdf, doc_id))
    path, _ = export_mod.export(doc, SRC, tmp_path_factory.mktemp(doc_id))
    return doc, [json.loads(s) for s in path.read_text(encoding="utf-8").splitlines()]


def test_ar004_results_section_rejoined(extract_mod, clean_mod, export_mod, tmp_path_factory):
    if AR004_PDF is None:
        pytest.skip("找不到 AR004 原始 PDF")
    doc, records = _records(extract_mod, clean_mod, export_mod, AR004_PDF, "AR004", tmp_path_factory)
    joined = [" ".join(r["display_text"].split()) for r in records]
    assert not any("Vol. 36, No. 1 this study" in t or "complex- 1 This work" in t for t in joined)
    results = [r for r in records if r["display_text"].startswith("All the redness, yellowness, and lightness")]
    assert len(results) == 1 and results[0]["pages"] == [5, 6, 7, 8]
    text = " ".join(results[0]["display_text"].split())
    assert "(F(4, 480)＝ 104.43" in text and "Note. D.F." not in text and "vs Perceptual" not in text
    reasons = classify_blocks(doc)
    exported_heads = [b for b in doc.blocks if reasons[b.block_id] is None and "Journal of Psychonomic Science Vol" in b.cleaned_text]
    assert exported_heads == []


def test_ar002_sphericity_rejoined(extract_mod, clean_mod, export_mod, tmp_path_factory):
    if AR002_PDF is None:
        pytest.skip("找不到 AR002 原始 PDF")
    _, records = _records(extract_mod, clean_mod, export_mod, AR002_PDF, "AR002", tmp_path_factory)
    joined = [" ".join(r["display_text"].split()) for r in records]
    assert any("if the Sphericity assumption was violated" in t for t in joined)
    assert not any("Sphericity Display of the lipstick" in t for t in joined)


def test_ar012_first_page_sentence_rejoined(extract_mod, clean_mod, export_mod, tmp_path_factory):
    if AR012_PDF is None:
        pytest.skip("找不到 AR012 原始 PDF")
    _, records = _records(extract_mod, clean_mod, export_mod, AR012_PDF, "AR012", tmp_path_factory)
    hits = [r for r in records if "due to its material characteristics, and Vergnaud" in " ".join(r["display_text"].split())]
    assert len(hits) == 1 and hits[0]["pages"] == [1, 2]
