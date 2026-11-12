"""解析完整性測試：表格擷取、欄位對齊、失敗紀錄、資料可追溯性。

- 合成 PDF（tests/fixtures/synthetic_pdf.py）：驗證可解析表格的儲存格、欄名、列名、單位、對齊與空值。
- Leeds 2024 全文：驗證圖片表格、向量圖表、公式都留下失敗紀錄，而且每筆紀錄都能追溯到頁碼與 bbox。
"""

from __future__ import annotations

import pydantic
import pytest

from schemas import Document, SourceManifest, TextBlock
from tests.fixtures.synthetic_pdf import (
    BORDERLESS_CAPTION,
    BORDERLESS_HEADER,
    BORDERLESS_ROWS,
    IMAGE_CAPTION,
    RULED_CAPTION,
    RULED_HEADER,
    RULED_ROWS,
)

LEEDS_TABLE_PAGES = {"Table 4.1": 42, "Table 4.2": 44, "Table 4.3": 50, "Table 4.4": 52,
                     "Table 4.5": 55, "Table 4.6": 58, "Table 4.7": 61, "Table 4.8": 62}


def _tables(doc):
    return [b for b in doc.blocks if b.block_type == "table"]


def _table_by_caption(doc, caption):
    return next(b for b in _tables(doc) if b.table.caption == caption)


def _empty_to_none(rows):
    return [[c if c else None for c in r] for r in rows]


# ---------------------------------------------------------------------------
# 表格擷取（合成 PDF）
# ---------------------------------------------------------------------------


def test_ruled_table_cells_exact(synthetic_extracted):
    t = _table_by_caption(synthetic_extracted, RULED_CAPTION)
    assert t.table.detection_method == "pymupdf_lines"
    assert _empty_to_none(t.table_data) == _empty_to_none([RULED_HEADER] + RULED_ROWS)


def test_ruled_table_structure(synthetic_extracted):
    t = _table_by_caption(synthetic_extracted, RULED_CAPTION).table
    assert t.column_names == RULED_HEADER
    assert t.row_labels == [r[0] for r in RULED_ROWS]
    assert t.header_rows == 1 and (t.n_rows, t.n_cols) == (5, 5)
    assert t.units == {"4": "°"}            # 來自欄名 'h (°)'；其他欄沒有標示單位就不補
    assert t.text_coverage == 1.0


def test_borderless_table_aligned_by_word_coordinates(synthetic_extracted):
    t = _table_by_caption(synthetic_extracted, BORDERLESS_CAPTION)
    assert t.table.detection_method == "pymupdf_words"
    assert t.table_data == [BORDERLESS_HEADER] + BORDERLESS_ROWS   # "Cohen's d" 沒被拆開或截斷
    assert t.table.column_names == BORDERLESS_HEADER
    assert t.table.row_labels == ["Light Orange", "Red", "Purple"]
    assert not t.needs_review


@pytest.mark.parametrize("caption", [RULED_CAPTION, BORDERLESS_CAPTION])
def test_cell_bboxes_columns_do_not_overlap(synthetic_extracted, caption):
    """欄位對齊：同一欄的儲存格都要落在該欄，且第 j 欄整體位於第 j+1 欄左側。"""
    t = _table_by_caption(synthetic_extracted, caption).table
    boxes = t.cell_bboxes
    assert boxes and all(len(r) == t.n_cols for r in boxes)
    for j in range(t.n_cols - 1):
        right_of_j = max(r[j][2] for r in boxes if r[j])
        left_of_next = min(r[j + 1][0] for r in boxes if r[j + 1])
        assert right_of_j <= left_of_next + 0.5, (caption, j)


def test_caption_linked_both_ways(synthetic_extracted):
    by_id = {b.block_id: b for b in synthetic_extracted.blocks}
    for caption in (RULED_CAPTION, BORDERLESS_CAPTION):
        t = _table_by_caption(synthetic_extracted, caption)
        cap = by_id[t.table.caption_block_id]
        assert cap.block_type == "table_caption" and t.block_id in cap.linked_block_ids
        assert cap.page_number == t.page_number == 1


def test_blank_cell_kept_empty_not_guessed(synthetic_extracted, synthetic_cleaned):
    raw = _table_by_caption(synthetic_extracted, RULED_CAPTION)
    cleaned = next(b for b in synthetic_cleaned.blocks if b.block_id == raw.block_id)
    coral_raw = next(r for r in raw.table_data if r[0] == "Coral")
    coral_clean = next(r for r in cleaned.cleaned_table_data if r[0] == "Coral")
    assert not coral_raw[3] and not coral_clean[3]          # b* 原表空白 → 仍是空白
    assert raw.table.empty_cells == 1
    assert raw.needs_review and any("未補值" in r for r in raw.review_reasons)


def test_cleaning_does_not_change_table_values(synthetic_extracted, synthetic_cleaned):
    for raw in _tables(synthetic_extracted):
        cleaned = next(b for b in synthetic_cleaned.blocks if b.block_id == raw.block_id)
        assert [[c or None for c in r] for r in cleaned.cleaned_table_data] == \
               [[(" ".join(c.split()) if c else None) for c in r] for r in raw.table_data]
    values = {c for b in _tables(synthetic_cleaned) for r in b.cleaned_table_data for c in r if c}
    assert {"45.32", "-3.12", "355.0", "0.92", "h (°)", "L*", "a*", "b*"} <= values


def test_page_text_fully_accounted(synthetic_extracted):
    assert [p.text_coverage for p in synthetic_extracted.pages] == [1.0, 1.0]


def test_table_coverage_detects_dropped_text(extract_mod):
    import pymupdf
    words = [(72, 100, 110, 110, "Cohen's"), (113, 100, 118, 110, "d")]
    rect = pymupdf.Rect(70, 98, 120, 112)
    assert extract_mod.table_text_coverage(rect, words, [["Cohen's d"]]) == 1.0
    assert extract_mod.table_text_coverage(rect, words, [["Cohen's"]]) < 1.0   # 少了 'd' 要被抓到


# ---------------------------------------------------------------------------
# 失敗紀錄
# ---------------------------------------------------------------------------


def test_image_table_recorded_as_failed_region(synthetic_extracted):
    region = next(b for b in synthetic_extracted.blocks if b.block_type == "image_region")
    assert region.region_kind == "table_image" and region.parse_status == "failed"
    assert region.needs_review and region.failure_reason and region.raw_text == ""
    assert region.page_number == 2 and region.bbox is not None
    issue = next(i for i in synthetic_extracted.issues if i.issue_type == "table_not_machine_readable")
    assert issue.block_id == region.block_id and issue.page_number == 2
    assert issue.details["caption"] == IMAGE_CAPTION and "bbox" in issue.details
    assert synthetic_extracted.pages[1].status == "needs_review"


def test_failed_region_not_treated_as_blank_after_cleaning(synthetic_extracted, synthetic_cleaned):
    region = next(b for b in synthetic_cleaned.blocks if b.block_type == "image_region")
    assert not region.is_removed                       # 不會被當成「空白內容」刪掉
    assert region.parse_status == "failed" and region.needs_review
    assert "unparsed_region_kept" in region.cleaning_ops


def _block(**kw):
    base = dict(doc_id="d", block_id="d_b1", block_type="paragraph", order=0)
    return TextBlock(**{**base, **kw})


def test_schema_rejects_silent_empty_block():
    with pytest.raises(pydantic.ValidationError):
        _block(raw_text="")                                              # 空白卻宣稱 ok
    with pytest.raises(pydantic.ValidationError):
        _block(block_type="image_region", raw_text="", parse_status="failed")  # 沒說原因
    ok = _block(block_type="image_region", raw_text="", parse_status="failed",
                failure_reason="image", needs_review=True)
    assert ok.parse_status == "failed"


def test_document_rejects_dangling_references():
    m = SourceManifest(doc_id="d", source_path="x", source_type="pdf", sha256="0" * 64,
                       file_size_bytes=0, extractor="t")
    with pytest.raises(pydantic.ValidationError):
        Document(stage="extracted", manifest=m, blocks=[_block(raw_text="x", linked_block_ids=["d_missing"])])


# ---------------------------------------------------------------------------
# Leeds 2024 全文
# ---------------------------------------------------------------------------


def test_leeds_all_image_tables_flagged(leeds_full):
    by_id = {b.block_id: b for b in leeds_full.blocks}
    caps = {" ".join(b.raw_text.split())[:9]: b for b in leeds_full.blocks if b.block_type == "table_caption"}
    assert set(caps) == set(LEEDS_TABLE_PAGES)
    for label, page in LEEDS_TABLE_PAGES.items():
        cap = caps[label]
        assert cap.page_number == page
        regions = [by_id[i] for i in cap.linked_block_ids if by_id[i].block_type == "image_region"]
        assert len(regions) == 1, label
        r = regions[0]
        assert r.region_kind == "table_image" and r.parse_status == "failed" and r.page_number == page
    assert not _tables(leeds_full)       # 沒有把向量圖表誤當表格輸出


def test_leeds_charts_rejected_as_tables_with_bbox(leeds_full):
    low = [i for i in leeds_full.issues if i.issue_type == "table_low_confidence"]
    assert low and {i.page_number for i in low} <= set(range(43, 58))
    assert all(i.details.get("bbox") and i.details.get("fill_ratio") for i in low)
    charts = [b for b in leeds_full.blocks if b.block_type == "chart_region"]
    assert charts and all(b.parse_status == "failed" and b.bbox for b in charts)


def test_leeds_formulas_linked_and_flagged(leeds_full):
    by_id = {b.block_id: b for b in leeds_full.blocks}
    labels = {" ".join(b.raw_text.split()): b for b in leeds_full.blocks if b.block_type == "equation_label"}
    assert {"Equation 2.1", "Equation 2.2", "Equation 2.3", "Equation 2.4", "Equation 3.2"} <= set(labels)
    for lab in labels.values():
        assert any(by_id[i].block_type == "formula" for i in lab.linked_block_ids), lab.raw_text
    formulas = [b for b in leeds_full.blocks if b.block_type == "formula"]
    assert formulas and all(f.needs_review and f.parse_status == "partial" and f.failure_reason for f in formulas)


def test_leeds_formula_text_not_altered_by_cleaning(clean_mod, leeds_full, leeds_full_cleaned):
    by_id = {b.block_id: b for b in leeds_full_cleaned.blocks}
    for f in (b for b in leeds_full.blocks if b.block_type == "formula"):
        c = by_id[f.block_id]
        assert c.cleaned_text == "\n".join(ln.strip() for ln in f.raw_text.splitlines() if ln.strip())
        assert clean_mod.verify_content_preserved(f.raw_text, c.cleaned_text) == []
    assert any("𝐿∗ = 116" in by_id[f.block_id].cleaned_text for f in leeds_full.blocks if f.block_type == "formula")


def test_leeds_every_review_item_traceable(leeds_full):
    doc_id = leeds_full.manifest.doc_id
    by_id = {b.block_id: b for b in leeds_full.blocks}
    review = [b for b in leeds_full.blocks if b.needs_review]
    assert review
    for b in review:
        assert b.doc_id == doc_id and b.block_id.startswith(f"{doc_id}_p{b.page_number:04d}_")
        assert b.page_number and b.bbox and b.review_reasons
        x0, y0, x1, y1 = b.bbox
        assert 0 <= x0 <= x1 <= 600 and 0 <= y0 <= y1 <= 845
        if b.parse_status != "ok":
            assert b.failure_reason
    for i in leeds_full.issues:
        assert i.page_number
        if i.block_id:
            assert by_id[i.block_id].page_number == i.page_number


def test_leeds_page_reports(leeds_full):
    assert [p.page_number for p in leeds_full.pages] == list(range(1, 95))
    assert all(p.text_coverage >= 0.98 for p in leeds_full.pages)
    review = {p.page_number for p in leeds_full.pages if p.status != "ok"}
    assert set(LEEDS_TABLE_PAGES.values()) | {13, 14, 15, 38, 39, 92, 93, 94} <= review
    assert {7, 8, 12, 20, 70} .isdisjoint(review)        # 純文字頁不應被標記


def test_integrity_report(report_mod, leeds_full_cleaned):
    rep = report_mod.build_integrity_report(leeds_full_cleaned)
    s = rep["summary"]
    assert s["pages_processed"] == 94
    assert s["pages_ok"] + s["pages_needs_review"] + s["pages_failed"] == 94
    assert s["tables_parsed"] == 0 and s["tables_unparsed"] == 8
    assert 42 in rep["pages_needing_review"] and 7 not in rep["pages_needing_review"]
    p42 = next(p for p in rep["pages"] if p["page_number"] == 42)
    entry = next(b for b in p42["review_blocks"] if b["region_kind"] == "table_image")
    assert entry["caption"].startswith("Table 4.1") and entry["bbox"] and entry["doc_id"] == "TH013"
    md = report_mod.render_markdown(rep)
    assert "待人工確認的頁面" in md and "Table 4.1" in md


def test_text_quality_scan_leeds(report_mod, leeds_full_cleaned):
    rep = report_mod.build_integrity_report(leeds_full_cleaned)
    by_rule = rep["summary"]["text_quality_by_rule"]
    assert by_rule["mass_glued_words"] == 0            # PyMuPDF 沒有 pypdf 式的大量黏字
    glued = {x["page_number"] for x in rep["text_quality"] if x["rule"] == "glued_term_in_source"}
    assert glued == {14}                               # 只有 p.14 的 Laboffers / Labcolour
    assert by_rule["glyph_mapping_residue"] >= 1       # p.34 的 𝐿"、𝑎"
    assert "文字品質掃描" in report_mod.render_markdown(rep)


def test_text_quality_scan_detects_mass_gluing(report_mod):
    m = SourceManifest(doc_id="t", source_path="x", source_type="pdf", sha256="0" * 64,
                       file_size_bytes=0, extractor="test")
    glued = ("Thispublicationcovers installation and operation of the controller. Readthemanualcarefully "
             "beforeservicingtheequipment and keep it for future reference near the unit at all times.")
    blk = TextBlock(doc_id="t", block_id="t_b000", block_type="paragraph", order=0, page_number=1,
                    raw_text=glued, cleaned_text=glued, search_text=glued)
    doc = Document(stage="cleaned", manifest=m, blocks=[blk])
    rules = {x["rule"] for x in report_mod.scan_text_quality(doc)}
    assert "mass_glued_words" not in rules             # 只有 3/21 個長字，不算大量黏字
    assert "very_long_token" in rules                  # beforeservicingtheequipment ≥ 25 字母

    heavy = "Thispublicationcovers Readthemanualcarefully Keepitforfuturereference " * 4
    blk2 = blk.model_copy(update={"block_id": "t_b001", "order": 1, "raw_text": heavy,
                                  "cleaned_text": heavy, "search_text": heavy})
    doc2 = Document(stage="cleaned", manifest=m, blocks=[blk2])
    assert "mass_glued_words" in {x["rule"] for x in report_mod.scan_text_quality(doc2)}


def test_integrity_report_synthetic_tables(report_mod, synthetic_cleaned):
    rep = report_mod.build_integrity_report(synthetic_cleaned)
    parsed = {t["caption"]: t for t in rep["tables_parsed"]}
    assert parsed[RULED_CAPTION]["units"] == {"4": "°"}
    assert parsed[BORDERLESS_CAPTION]["method"] == "pymupdf_words"
    assert rep["summary"]["tables_unparsed"] == 1
