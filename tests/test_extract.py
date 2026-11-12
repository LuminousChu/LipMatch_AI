"""01_extract 的測試：無法辨識的表格要記錄錯誤、原始資料夾不可寫入、頁碼解析。"""

from __future__ import annotations

import pytest

from tests.conftest import LEEDS_PDF, ROOT


@pytest.fixture(scope="module")
def table_pages(extract_mod):
    if not LEEDS_PDF.exists():
        pytest.skip("找不到 TH013 原始 PDF")
    # p.42：Table 4.1（CIELAB 量測值）是嵌入的圖片；p.43：向量散佈圖被 find_tables 誤判為表格
    return extract_mod.extract_pdf(LEEDS_PDF, "TH013", "42-43")


def test_image_table_logged_for_manual_review(table_pages):
    issue = next(i for i in table_pages.issues if i.issue_type == "table_not_machine_readable")
    assert issue.page_number == 42 and issue.severity == "error"
    assert "Table 4.1" in issue.details["caption"]
    region = next(b for b in table_pages.blocks if b.block_id == issue.block_id)
    assert region.block_type == "image_region" and region.region_kind == "table_image"
    assert region.needs_review and region.parse_status == "failed"
    assert issue.details["caption_block_id"] in region.linked_block_ids


def test_chart_misdetected_as_table_is_rejected(table_pages):
    low = [i for i in table_pages.issues if i.issue_type == "table_low_confidence"]
    assert low and all(i.page_number == 43 for i in low)
    assert not any(b.block_type == "table" for b in table_pages.blocks)
    # 圖表軸標籤保留為 figure_text 並待確認，而不是被丟掉
    fig = [b for b in table_pages.blocks if b.block_type == "figure_text"]
    assert fig and all(b.needs_review for b in fig)


def test_prose_next_to_figures_stays_paragraph(table_pages):
    para = next(b for b in table_pages.blocks if b.raw_text.startswith("The skin and lip colours measured"))
    assert para.block_type == "paragraph" and para.section.endswith("4.1 Colour Measurement")


def test_refuses_to_write_into_raw_dir(extract_mod, clean_mod):
    with pytest.raises(ValueError):
        extract_mod.ensure_outside_raw(ROOT / "data" / "raw" / "out")
    with pytest.raises(ValueError):
        clean_mod.ensure_outside_raw(ROOT / "data" / "raw")
    extract_mod.ensure_outside_raw(ROOT / "data" / "interim")  # 不應拋錯


@pytest.mark.parametrize("spec, expected", [("1-3", [1, 2, 3]), ("1,3,5-6", [1, 3, 5, 6]), (None, [1, 2, 3, 4, 5, 6])])
def test_parse_page_spec(extract_mod, spec, expected):
    assert extract_mod.parse_page_spec(spec, 6) == expected


def test_parse_page_spec_out_of_range(extract_mod):
    with pytest.raises(ValueError):
        extract_mod.parse_page_spec("3-9", 4)


def test_block_ids_stable_across_runs(extract_mod):
    if not LEEDS_PDF.exists():
        pytest.skip("找不到 TH013 原始 PDF")
    a = extract_mod.extract_pdf(LEEDS_PDF, "TH013", "11-12")
    b = extract_mod.extract_pdf(LEEDS_PDF, "TH013", "11-12")
    assert [x.model_dump() for x in a.blocks] == [x.model_dump() for x in b.blocks]
