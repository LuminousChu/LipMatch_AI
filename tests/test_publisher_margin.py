"""期刊版面（SYNTHETIC）：右緣直排下載浮水印、頁頂表格標題、表格本體無法解析時的殘片。

對應 AR012（Wiley）實際問題：浮水印被併進右欄正文、頁頂的「TABLE 5」被當成頁首、
表格儲存格被拆成短段落後當成可用證據。
"""
from __future__ import annotations

import pytest

from lipmatch.eligibility import classify_blocks
from tests.fixtures.synthetic_pdf import FRAGMENTS, TOP_CAPTION, build_publisher_margin_pdf


@pytest.fixture(scope="module")
def margin_doc(extract_mod, tmp_path_factory):
    pdf = build_publisher_margin_pdf(tmp_path_factory.mktemp("publisher") / "publisher.pdf")
    return extract_mod.extract_pdf(pdf, "SYN_PUBLISHER")


@pytest.fixture(scope="module")
def margin_cleaned(clean_mod, margin_doc):
    return clean_mod.clean_document(margin_doc)


def test_watermark_not_merged_into_body(margin_doc):
    body = [b for b in margin_doc.blocks if b.block_type not in ("page_header", "page_footer")]
    assert not any("Downloaded from" in b.raw_text for b in body)
    right = [b for b in body if "P1R" in b.raw_text]
    assert right and all("SYNTHETIC 2025" not in b.raw_text for b in right)


def test_watermark_is_margin_text_and_removed_when_repeated(margin_doc, margin_cleaned):
    wm = [b for b in margin_doc.blocks if "Downloaded from" in b.raw_text]
    assert len(wm) == 3 and {b.block_type for b in wm} == {"page_footer"}
    cleaned = [b for b in margin_cleaned.blocks if "Downloaded from" in b.raw_text]
    assert all(b.is_removed for b in cleaned)


def test_caption_at_page_top_is_not_header(margin_doc):
    cap = [b for b in margin_doc.blocks if b.raw_text.strip().startswith("TABLE 5")]
    assert len(cap) == 1 and cap[0].block_type == "table_caption"
    assert cap[0].raw_text.strip() == TOP_CAPTION


def test_rotated_text_inside_page_is_kept(margin_doc):
    rot = [b for b in margin_doc.blocks if "Rotated axis title" in b.raw_text]
    assert rot and all(b.block_type not in ("page_header", "page_footer") for b in rot)
    assert all("P3R" not in b.raw_text for b in rot)  # 也不會和水平正文黏在一起


def test_orphan_table_fragments_need_review_and_are_not_exported(margin_cleaned):
    frags = [b for b in margin_cleaned.blocks if b.page_number == 3 and b.raw_text.strip() in FRAGMENTS]
    assert len(frags) >= 8
    assert all(b.needs_review and any("表格的殘片" in r for r in b.review_reasons) for b in frags)
    reasons = classify_blocks(margin_cleaned)
    assert all(reasons[b.block_id] is not None for b in frags)
    # 同頁的正常正文仍可用
    body = [b for b in margin_cleaned.blocks if b.page_number == 3 and "P3L" in b.raw_text]
    assert body and all(reasons[b.block_id] is None for b in body)
