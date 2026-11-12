"""TX001 章節路徑（C1）與段落混合（C2）修正（2026-10-17）。

對應問題（docs/mvp/evidence-coverage-tx001-2026-10-17.md §5）：
- C1：頁邊欄每頁都有的粗體「Notes」被當成章節標題，p.35 第二項起、p.95 全頁章節變成「Notes」；
  p.26「15.～19.」用較大字級排版的編號條列被當成標題，p.28 章節變成「the client's satisfaction with」。
- C2：p.35「2. The old jewellery trick …」後面接了原文另起的兩段（色輪用途、冷配冷暖配暖通則）。

合成資料標示 SYNTHETIC；真實 PDF 測試在檔案不存在時略過。
"""
from __future__ import annotations

import pymupdf
import pytest

from tests.conftest import ROOT

TX001_PDF = next(iter(sorted((ROOT / "data" / "raw").rglob("TX001_*.pdf"))), None)
AR002_PDF = next(iter(sorted((ROOT / "data" / "raw").rglob("AR002_*.pdf"))), None)


def _line(extract_mod, text, x0, y0, x1=537.9, size=12.0, first_bold=False, col=None):
    return extract_mod.Line(text=text, bbox=pymupdf.Rect(x0, y0, x1, y0 + 15), size=size, bold_all=first_bold,
                            first_bold=first_bold, col=col)


# ---------------------------------------------------------------------------
# C1：頁面標籤
# ---------------------------------------------------------------------------

def _label_pdf(path, pages=6, extra_heading_page=None):
    """SYNTHETIC：每頁頁邊欄同一位置有「Notes」，另有正文；可在某一頁別的位置放一個「Notes」標題。"""
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((60, 200), "Notes", fontsize=12)
        page.insert_text((200, 200), f"SYNTHETIC body text on page {i + 1} for the label test.", fontsize=11)
        if extra_heading_page == i:
            page.insert_text((400, 400), "Notes", fontsize=14)
    doc.save(path)
    doc.close()
    return path


def test_running_label_found(extract_mod, tmp_path):
    with pymupdf.open(_label_pdf(tmp_path / "labels.pdf")) as pdf:
        labels = extract_mod.find_running_labels(pdf, list(range(1, pdf.page_count + 1)))
    assert [t for t, _ in labels] == ["Notes"]
    assert extract_mod.is_running_label("Notes", pymupdf.Rect(labels[0][1]), labels)


def test_label_on_too_few_pages_ignored(extract_mod, tmp_path):
    """只出現在 2 頁（< 3 頁）不算頁面標籤。"""
    with pymupdf.open(_label_pdf(tmp_path / "few.pdf", pages=2)) as pdf:
        assert extract_mod.find_running_labels(pdf, [1, 2]) == []


def test_spot_on_two_pages_not_label(extract_mod, tmp_path):
    """同一文字：位置 A 出現 4 頁（是標籤），位置 B 只出現 2 頁 → 位置 B 不算標籤。"""
    doc = pymupdf.open()
    for i in range(6):
        page = doc.new_page(width=595, height=842)
        page.insert_text((60, 200) if i < 4 else (400, 400), "Notes", fontsize=12)
        page.insert_text((200, 300), f"SYNTHETIC body text on page {i + 1}.", fontsize=11)
    doc.save(tmp_path / "spots.pdf")
    doc.close()
    with pymupdf.open(tmp_path / "spots.pdf") as pdf:
        labels = extract_mod.find_running_labels(pdf, list(range(1, 7)))
    assert len(labels) == 1 and labels[0][1].x0 < 100


def test_same_text_other_position_not_label(extract_mod, tmp_path):
    """同一個字出現在別的位置（例如只出現一次的「Notes」標題）不是頁面標籤。"""
    with pymupdf.open(_label_pdf(tmp_path / "extra.pdf", extra_heading_page=2)) as pdf:
        labels = extract_mod.find_running_labels(pdf, list(range(1, pdf.page_count + 1)))
    assert not extract_mod.is_running_label("Notes", pymupdf.Rect(400, 388, 440, 404), labels)


def test_running_label_becomes_page_header_not_section(extract_mod, tmp_path):
    with pymupdf.open(_label_pdf(tmp_path / "doc.pdf")) as pdf:
        pass
    doc = extract_mod.extract_pdf(tmp_path / "doc.pdf", "SYN_LABEL")
    notes = [b for b in doc.blocks if b.raw_text.strip() == "Notes"]
    assert notes and all(b.block_type == "page_header" for b in notes)
    assert not any("Notes" in (b.section or "") for b in doc.blocks)


# ---------------------------------------------------------------------------
# C1：句子沒寫完的編號條列不是標題
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("19. Ask questions to check the client's satisfaction with", True),
    ("Toned colours generally appeal pleasing to", True),
    ("3.5. The evaluation of color stripes on the forearm", False),
    ("Significance of Colour Wheel to Make-up", False),
    ("研究方法與", False),            # 只看英文
])
def test_ends_unfinished(extract_mod, text, expected):
    assert extract_mod.ends_unfinished(text) is expected


def test_numbered_sentence_with_continuation_demoted(extract_mod):
    lines = [_line(extract_mod, "15.\tMinimise the wastage of products by using products", 84, 100),
             _line(extract_mod, "economically, and following correct storage", 102, 113),
             _line(extract_mod, "procedures as per the manufacturer's instructions.", 102, 126)]
    kinds = extract_mod.demote_unfinished_numbered_headings(lines, ["heading", "text", "text"])
    assert kinds[0] == "list_item"


def test_numbered_line_ending_with_period_demoted(extract_mod):
    lines = [_line(extract_mod, "16. Inform waiting customers of time left to service periodically.", 84, 100)]
    assert extract_mod.demote_unfinished_numbered_headings(lines, ["heading"]) == ["list_item"]


def test_two_line_real_heading_kept(extract_mod):
    """AR002 p.7 的兩行標題：續行沒有句號，仍是標題。"""
    lines = [_line(extract_mod, "3.5. The evaluation of color stripes on the", 50, 100),
             _line(extract_mod, "forearm", 50, 113, x1=90),
             _line(extract_mod, "4. SYNTHETIC next heading", 50, 140)]
    kinds = extract_mod.demote_unfinished_numbered_headings(lines, ["heading", "heading", "heading"])
    assert kinds == ["heading", "heading", "heading"]


def test_bold_numbered_heading_kept(extract_mod):
    lines = [_line(extract_mod, "2. Colour basics.", 50, 100, first_bold=True)]
    assert extract_mod.demote_unfinished_numbered_headings(lines, ["heading"]) == ["heading"]


def test_continuation_far_below_not_counted(extract_mod):
    """下一行離太遠（不是續行）：不能用它的句號判斷。"""
    lines = [_line(extract_mod, "5. SYNTHETIC Results overview", 50, 100),
             _line(extract_mod, "This paragraph starts after a gap.", 50, 160)]
    assert extract_mod.demote_unfinished_numbered_headings(lines, ["heading", "text"])[0] == "heading"


# ---------------------------------------------------------------------------
# C2：條列之後的段落、首行偏移的新段落
# ---------------------------------------------------------------------------

def test_outdented_line_after_list_item_is_new_paragraph(extract_mod):
    item = [_line(extract_mod, "2.\t The old jewellery trick: SYNTHETIC", 226.6, 164),
            _line(extract_mod, "are suitable for the client, the undertone is cool.", 244.8, 227, x1=518)]
    assert extract_mod.outdented_after_list_item("list_item", item, _line(extract_mod, "The colour wheel helps", 210.8, 243))
    assert not extract_mod.outdented_after_list_item("list_item", item, _line(extract_mod, "be determined whether", 244.8, 180))
    assert not extract_mod.outdented_after_list_item("text", item, _line(extract_mod, "The colour wheel helps", 210.8, 243))


def _para(extract_mod):
    return [_line(extract_mod, "\t\t The colour wheel helps identify the suitable", 210.8, 243),
            _line(extract_mod, "colour for the foundation, concealer, blush, eyeliner,", 244.8, 259),
            _line(extract_mod, "and lipstick.", 244.8, 275, x1=314)]


def test_offset_paragraph_split(extract_mod):
    nxt = _line(extract_mod, "\t\t The basic rule is to stick to cool colours", 210.8, 291)
    assert extract_mod.starts_new_offset_paragraph(_para(extract_mod), nxt)


@pytest.mark.parametrize("change", ["no_offset", "no_period", "long_last_line", "not_at_first_x0"])
def test_offset_paragraph_counterexamples(extract_mod, change):
    lines = _para(extract_mod)
    nxt = _line(extract_mod, "\t\t The basic rule is to stick to cool colours", 210.8, 291)
    if change == "no_offset":            # 沒有首行偏移的版式：第一行與續行同左緣
        lines[0] = _line(extract_mod, "The colour wheel helps identify the suitable", 244.8, 243)
        nxt = _line(extract_mod, "The basic rule is to stick", 244.8, 291)
    elif change == "no_period":          # 上一行沒有句號
        lines[2] = _line(extract_mod, "and lipstick", 244.8, 275, x1=314)
    elif change == "long_last_line":     # 上一行寫滿（不是段落最後一行）
        lines[2] = _line(extract_mod, "and lipstick for every SYNTHETIC client in the salon.", 244.8, 275)
    else:                                # 這一行不在第一行的位置
        nxt = _line(extract_mod, "The basic rule is to stick", 244.8, 291)
    assert not extract_mod.starts_new_offset_paragraph(lines, nxt)


# ---------------------------------------------------------------------------
# 真實文件
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def tx001_doc(extract_mod):
    if TX001_PDF is None:
        pytest.skip("找不到 TX001 原始 PDF")
    return extract_mod.extract_pdf(TX001_PDF, "TX001")


def test_tx001_c1_sections(tx001_doc):
    body = [b for b in tx001_doc.blocks if b.block_type in ("paragraph", "list_item")]
    by_page = lambda p: [b for b in body if b.page_number == p]
    assert not any("Notes" in (b.section or "").split(" > ") for b in by_page(35) + by_page(95))
    assert not any((b.section or "").rstrip().lower().endswith((" with", " to")) for b in body)
    assert all("General Guidelines for a Beauty Therapist" in (b.section or "") for b in by_page(28)[:4])


def test_tx001_c1_numbered_list_not_heading(tx001_doc):
    p26 = [b for b in tx001_doc.blocks if b.page_number == 26]
    item = [b for b in p26 if b.raw_text.lstrip().startswith("15.")]
    assert len(item) == 1 and item[0].block_type == "list_item"
    assert "manufacturer’s instructions." in " ".join(item[0].raw_text.split())


def test_tx001_c2_p35_split(tx001_doc):
    p35 = [" ".join(b.raw_text.split()) for b in tx001_doc.blocks if b.page_number == 35]
    item2 = [t for t in p35 if t.startswith("2. The old jewellery trick")]
    assert len(item2) == 1 and item2[0].endswith("the undertone is cool.")
    assert any(t.startswith("The colour wheel helps") and t.endswith("and lipstick.") for t in p35)
    assert any(t.startswith("The basic rule is to stick to cool colours") for t in p35)


def test_ar002_two_line_heading_unchanged(extract_mod):
    if AR002_PDF is None:
        pytest.skip("找不到 AR002 原始 PDF")
    doc = extract_mod.extract_pdf(AR002_PDF, "AR002", "7")
    heads = [" ".join(b.raw_text.split()) for b in doc.blocks if b.block_type == "heading"]
    assert any(h.startswith("3.5. The evaluation of color stripes on the") for h in heads)


def test_objectives_region_skips_removed_page_footer():
    """學習目標條列中間夾著已移除的頁尾（TX001 p.16），條列區不中斷。"""
    from lipmatch.eligibility import objectives_block_ids
    from schemas import TextBlock

    def blk(i, text, bt="paragraph", removed=False):
        return TextBlock(doc_id="t", block_id=f"t_b{i:03d}", block_type=bt, order=i, page_number=16 + i // 3,
                         raw_text=text, cleaned_text=text, search_text=text, is_removed=removed,
                         removal_reason="page_footer:repeated_on_5_pages" if removed else None)
    blocks = [blk(0, "What have you learned?", bt="label"),
              blk(1, "• Explain the SYNTHETIC role of a beauty therapist"),
              blk(2, "SYNTHETIC Industry footer 5", removed=True),   # 擷取成段落、清洗時才移除的頁尾
              blk(3, "• Describe SYNTHETIC hygiene practices")]
    assert {"t_b001", "t_b003"} <= objectives_block_ids(blocks)


# ---------------------------------------------------------------------------
# 人工決定排除的內容（config/content_holds.json）：TX001 p.35 靜脈判斷段
# ---------------------------------------------------------------------------

import hashlib  # noqa: E402
import json  # noqa: E402

from lipmatch.eligibility import classify_blocks  # noqa: E402
from schemas import Document, SourceManifest, TextBlock  # noqa: E402

HOLD_TEXT = "1. SYNTHETIC vein test: blue means warm."


def _hold_doc(doc_id="SYN_HOLD", sha="a" * 64):
    m = SourceManifest(doc_id=doc_id, source_path="x.pdf", source_type="pdf", sha256=sha,
                       file_size_bytes=0, extractor="test")
    blocks = [TextBlock(doc_id=doc_id, block_id=f"{doc_id}_b{i:03d}", block_type="list_item", order=i, page_number=35,
                        bbox=(226, 100 + 70 * i, 538, 160 + 70 * i), raw_text=t)
              for i, t in enumerate([HOLD_TEXT, "2. SYNTHETIC jewellery trick: gold means warm."])]
    return Document(stage="extracted", manifest=m, blocks=blocks)


def _hold(**kw):
    h = {"doc_id": "SYN_HOLD", "source_sha256": "a" * 64, "page": 35, "text_start": "1. SYNTHETIC vein",
         "text_sha256": hashlib.sha256(HOLD_TEXT.encode()).hexdigest(), "reason": "SYNTHETIC 內容衝突排除"}
    h.update(kw)
    return h


def test_content_hold_excludes_only_target(clean_mod):
    doc = clean_mod.clean_document(_hold_doc(), holds=[_hold()])
    reasons = classify_blocks(doc)
    held, other = doc.blocks
    assert held.needs_review and any(r.startswith(clean_mod.CONTENT_HOLD_PREFIX) for r in held.review_reasons)
    assert reasons[held.block_id] == "needs_review" and reasons[other.block_id] is None
    assert held.cleaned_text == HOLD_TEXT          # 原文保留，不刪不改


def test_content_hold_other_doc_untouched(clean_mod):
    doc = clean_mod.clean_document(_hold_doc(doc_id="SYN_OTHER"), holds=[_hold()])
    assert not any(b.needs_review for b in doc.blocks)


@pytest.mark.parametrize("change", [{"source_sha256": "b" * 64}, {"text_sha256": "0" * 64}, {"page": 36}])
def test_content_hold_stale_config_raises(clean_mod, change):
    """來源或文字變了：不默默套用，直接報錯要求重新核對。"""
    with pytest.raises(ValueError):
        clean_mod.clean_document(_hold_doc(), holds=[_hold(**change)])


def test_tx001_vein_test_held_others_exported(clean_mod, tx001_doc):
    """整份擷取才有頁面標籤偵測；只擷取單頁時文字不同，設定會拒絕套用（防護本身另有測試）。"""
    doc = clean_mod.clean_document(tx001_doc)
    reasons = classify_blocks(doc)
    p35 = {" ".join(b.cleaned_text.split())[:30]: b for b in doc.blocks if b.page_number == 35}
    vein = next(b for k, b in p35.items() if k.startswith("1. Vein test"))
    assert reasons[vein.block_id] == "needs_review" and "content_hold" in vein.cleaning_ops
    for start in ("2. The old jewellery trick", "The colour wheel helps", "The basic rule is to stick"):
        b = next(b for k, b in p35.items() if k.startswith(start[:30]))
        assert "content_hold" not in b.cleaning_ops


# ---------------------------------------------------------------------------
# 圖表標題的續行接回標題、匯出成同一筆（第四批 mark_caption_variants 的誤拆修正）
# ---------------------------------------------------------------------------

def _cap_doc(cap_review=False):
    m = SourceManifest(doc_id="SYN_CAP", source_path="x.pdf", source_type="pdf", sha256="c" * 64,
                       file_size_bytes=0, extractor="test")
    kw = {"needs_review": True, "review_reasons": ["SYNTHETIC orphan caption"]} if cap_review else {}
    blocks = [TextBlock(doc_id="SYN_CAP", block_id="SYN_CAP_b000", block_type="paragraph", order=0, page_number=7,
                        bbox=(48, 304, 470, 325), raw_text="Table 3. SYNTHETIC relationships: Pearson's coefficients of correlation", **kw),
              TextBlock(doc_id="SYN_CAP", block_id="SYN_CAP_b001", block_type="paragraph", order=1, page_number=7,
                        bbox=(48, 324, 137, 335), raw_text="and F-values in parenthesis.")]
    return Document(stage="extracted", manifest=m, blocks=blocks)


def test_caption_tail_exported_with_caption(clean_mod, tmp_path):
    from lipmatch.sources import SourceInfo
    exp = _load_export()
    doc = clean_mod.clean_document(_cap_doc(), holds=[])
    path, _ = exp.export(doc, SourceInfo(title="S", authors=["T"], author_citation="T", year=2026, verified_from="s"), tmp_path)
    records = [json.loads(s) for s in path.read_text(encoding="utf-8").splitlines()]
    caps = [r for r in records if r["block_type"] == "table_caption"]
    assert len(caps) == 1 and caps[0]["block_ids"] == ["SYN_CAP_b000", "SYN_CAP_b001"]
    assert caps[0]["display_text"].endswith("coefficients of correlation and F-values in parenthesis.")


def test_caption_tail_held_with_review_caption(clean_mod):
    doc = clean_mod.clean_document(_cap_doc(cap_review=True), holds=[])
    assert doc.blocks[1].needs_review


def _load_export():
    from tests.conftest import _load
    return _load("lipmatch_export_caption", "05_export_rag_ready.py")
