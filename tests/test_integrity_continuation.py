"""資料完整性修復第一批（2026-10-17）：數字／大寫開頭的續段、段落中途誤切成列表。

對應問題（2026-10-17 文件審核發現，ETL 修復時重現）：
- D01 AR015 p.5→6：「… = 4.99 for African American women,」→ 換頁、插圖 →「4.03 for Hispanic …」沒接上。
- D02 AR015 p.7：左欄底「… Hispanic American, African」→ 右欄「American and Asian Chinese.」沒接上。
- D07 AR012 p.6／p.12：段落換行後恰好以「40.」「f.」開頭，被切成新的列表項。

除正例外，每條規則都有反例：新章節、句尾標點、列表編號、圖表標題、不在欄底、待確認、
略過 figure_text、第 1 頁、頁碼、非獨立英文字，確認沒有為了讓兩例通過而放寬成「所有數字或大寫開頭都接」。
合成資料標示 SYNTHETIC；真實 PDF 測試在檔案不存在時略過。
"""
from __future__ import annotations

import json

import pymupdf
import pytest

from lipmatch.sources import SourceInfo
from schemas import Document, SourceManifest, TextBlock
from tests.conftest import ROOT, _load

SRC = SourceInfo(title="SYNTHETIC", authors=["Tester"], author_citation="Tester", year=2026, verified_from="synthetic")
AR015_PDF = next(iter(sorted((ROOT / "data" / "raw").rglob("AR015_*.pdf"))), None)
AR012_PDF = next(iter(sorted((ROOT / "data" / "raw").rglob("AR012_*.pdf"))), None)

LEFT, RIGHT = (46.0, 291.0), (307.0, 552.0)    # 雙欄版面的左右欄 x 範圍


@pytest.fixture(scope="module")
def export_mod():
    return _load("lipmatch_export_integrity", "05_export_rag_ready.py")


def _blk(i, text, bt="paragraph", page=2, col=LEFT, y=(400.0, 739.0), **kw):
    """SYNTHETIC 區塊：預設在第 2 頁左欄、下緣 739（欄底）。"""
    return TextBlock(doc_id="t", block_id=f"t_b{i:03d}", block_type=bt, order=i, page_number=page,
                     bbox=(col[0], y[0], col[1], y[1]), section="SYNTHETIC", raw_text=text, cleaned_text=text,
                     search_text=text, **kw)


def _doc(blocks):
    m = SourceManifest(doc_id="t", source_path="x", source_type="pdf", sha256="0" * 64,
                       file_size_bytes=0, extractor="test")
    return Document(stage="cleaned", manifest=m, blocks=blocks)


PREV_COMMA = "SYNTHETIC standard deviation on L* = 4.99 for group A women,"
PREV_PROPER = ("SYNTHETIC panels were recruited in four groups: Caucasian American and French, "
               "Hispanic American, African")


# ---------------------------------------------------------------------------
# 正例
# ---------------------------------------------------------------------------

def test_number_start_after_comma_across_page_and_figure(clean_mod):
    """D01 的形狀：前段在第 5 頁右欄底以逗號結尾；第 6 頁先是圖片與圖說，再接數字開頭的續段。"""
    blocks = [
        _blk(0, PREV_COMMA, page=5, col=RIGHT),
        _blk(1, "", bt="image_region", page=6, y=(47, 387), parse_status="failed", needs_review=True,
             failure_reason="SYNTHETIC image", review_reasons=["SYNTHETIC image"]),
        _blk(2, "FIGURE 3 SYNTHETIC boxplots.", bt="figure_caption", page=6, y=(400, 448)),
        _blk(3, "4.03 for group B women and 3.16 for group C women.", page=6, y=(465, 583)),
    ]
    assert clean_mod.link_cross_page_continuations(blocks) == 1
    assert blocks[3].continues_from_block_id == "t_b000"
    assert "linked_by_sentence_end_evidence" in blocks[3].cleaning_ops


def test_proper_noun_split_across_columns(clean_mod):
    """D02 的形狀：左欄底停在「African」，右欄接「American and Asian Chinese.」。"""
    blocks = [_blk(0, PREV_PROPER, y=(699, 739)),
              _blk(1, "American and Asian Chinese. Following the SYNTHETIC measurements.", col=RIGHT, y=(673, 739))]
    assert clean_mod.link_cross_page_continuations(blocks) == 1


def test_connector_word_then_table_reference(clean_mod):
    """TH013 p.67→68：「As delineated in」→「Table 4.1, the hue angle …」（句中引用表格，後面是逗號）。"""
    blocks = [_blk(0, "SYNTHETIC outcomes for the lipsticks. As delineated in", page=67, col=(70, 530)),
              _blk(1, "Table 4.1, the hue angle is found to be more significant.", page=68, col=(70, 530), y=(72, 200))]
    assert clean_mod.link_cross_page_continuations(blocks) == 1


def test_merged_record_keeps_both_pages(clean_mod, export_mod, tmp_path):
    """接上後 05 匯出成一筆，保留兩頁來源與兩個 block_id，沒有懸空續段。"""
    blocks = [_blk(0, PREV_COMMA, page=5, col=RIGHT),
              _blk(1, "4.03 for group B women and 3.16 for group C women.", page=6, y=(465, 583))]
    clean_mod.link_cross_page_continuations(blocks)
    path, _ = export_mod.export(_doc(blocks), SRC, tmp_path)
    records = [json.loads(s) for s in path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1 and records[0]["pages"] == [5, 6]
    assert records[0]["block_ids"] == ["t_b000", "t_b001"]
    assert "women, 4.03 for group B" in records[0]["display_text"]
    assert not export_mod.dangling_continuations(records)


# ---------------------------------------------------------------------------
# 反例：文字條件
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("prev, cur", [
    ("SYNTHETIC results were measured.", "The second SYNTHETIC panel was larger."),       # 句尾標點：新句
    (PREV_COMMA, "1. Apply the SYNTHETIC lipstick evenly."),                                # 列表編號
    (PREV_COMMA, "a) SYNTHETIC first option for the panel."),                               # 字母編號
    ("SYNTHETIC values are summarized in", "TABLE 3 SYNTHETIC Pearson's r values"),        # 全大寫圖表標題
    ("SYNTHETIC values are summarized in", "Table 2: SYNTHETIC mean values by group"),     # 圖表標題格式
    ("SYNTHETIC lightness had no effect on perceptual lightness of", "13"),                 # 頁碼
    ("SYNTHETIC formula with wavelength λa", "Km is the maximum luminous efficacy"),        # λa 的 a 不是冠詞
    ("Skin Tone", "The SYNTHETIC next section starts here."),                              # 短標籤，非句中斷開
    ("SYNTHETIC samples were measured under daylight", "Results show a SYNTHETIC trend."),  # 小寫字結尾、非連接詞
])
def test_strong_continuation_text_counterexamples(clean_mod, prev, cur):
    assert clean_mod._is_strong_continuation(prev, cur) is False


def test_strong_continuation_text_positives(clean_mod):
    assert clean_mod._is_strong_continuation(PREV_COMMA, "4.03 for group B women.")
    assert clean_mod._is_strong_continuation(PREV_PROPER, "American and Asian Chinese.")
    assert clean_mod._is_strong_continuation("SYNTHETIC outcomes. As delineated in", "Table 4.1, the hue angle is")


# ---------------------------------------------------------------------------
# 反例：版面與狀態條件
# ---------------------------------------------------------------------------

def test_heading_between_resets(clean_mod):
    blocks = [_blk(0, PREV_COMMA, page=5, col=RIGHT),
              _blk(1, "3 SYNTHETIC Discussion", bt="heading", page=6, y=(40, 52), heading_level=1),
              _blk(2, "4.03 for group B women.", page=6, y=(60, 120))]
    assert clean_mod.link_cross_page_continuations(blocks) == 0


def test_not_at_column_bottom(clean_mod):
    """前段不是所在欄最後一段（下面還有正文）→ 不是換欄斷句，不接。"""
    prev = _blk(0, PREV_PROPER, y=(400, 500))
    below = _blk(1, "SYNTHETIC later paragraph in the same column.", y=(520, 739))
    other_col = _blk(2, "SYNTHETIC right column text.", col=RIGHT, y=(60, 739))
    bottoms = clean_mod._column_bottoms([prev, below, other_col])
    assert clean_mod._ends_its_column(prev, bottoms) is False      # 同欄下方還有正文
    assert clean_mod._ends_its_column(below, bottoms) is True       # 另一欄的下緣不影響本欄判斷


def test_not_at_column_bottom_not_linked(clean_mod):
    """接續流程確實套用欄底前提：前段下方同欄還有正文（閱讀順序在前），不接到右欄。"""
    below = _blk(0, "SYNTHETIC complete sentence lower in the left column.", y=(520, 739))
    prev = _blk(1, PREV_PROPER, y=(400, 500))
    cur = _blk(2, "American and Asian Chinese. Following the SYNTHETIC panel.", col=RIGHT, y=(60, 120))
    assert clean_mod.link_cross_page_continuations([below, prev, cur]) == 0


def test_needs_review_prev_not_linked(clean_mod):
    blocks = [_blk(0, PREV_COMMA, page=5, col=RIGHT, needs_review=True, review_reasons=["check"]),
              _blk(1, "4.03 for group B women.", page=6, y=(465, 583))]
    assert clean_mod.link_cross_page_continuations(blocks) == 0


def test_figure_text_between_not_linked(clean_mod):
    blocks = [_blk(0, PREV_COMMA, page=5, col=RIGHT),
              _blk(1, "4.5", bt="figure_text", page=6, y=(100, 110)),
              _blk(2, "4.03 for group B women.", page=6, y=(465, 583))]
    assert clean_mod.link_cross_page_continuations(blocks) == 0


def test_first_page_front_matter_not_linked(clean_mod):
    """AR002 p.1：作者單位「…, Darmstadt, Germany」後面是摘要「Lipstick is …」，不可接。"""
    blocks = [_blk(0, "SYNTHETIC Institute of Printing Science and Technology, Technische Universität Darmstadt, "
                      "Darmstadt, Germany", page=1),
              _blk(1, "Lipstick is one of the most commonly used SYNTHETIC cosmetics.", page=1, col=RIGHT)]
    assert clean_mod.link_cross_page_continuations(blocks) == 0


def test_side_caption_text_not_linked(clean_mod):
    """AR001 p.6：「FIGURE 6」標籤右邊的說明文字被擷取成段落，與標籤同高，不是正文續句。"""
    blocks = [_blk(0, "SYNTHETIC has an intermediate a* and C*. Except for the", page=5, col=RIGHT),
              _blk(1, "FIGURE 6", bt="figure_caption", page=6, col=(46, 88), y=(240, 248)),
              _blk(2, "Regions of interest defined on superior SYNTHETIC lips.", page=6, col=(46, 536), y=(240, 259))]
    assert clean_mod.link_cross_page_continuations(blocks) == 0


def test_lowercase_rule_unchanged(clean_mod):
    """原有小寫開頭規則不受新前提限制（例如不在欄底、第 1 頁也照接）。"""
    blocks = [_blk(0, "SYNTHETIC abstract text that stops in the middle of", page=1, y=(100, 200)),
              _blk(1, "a sentence and continues here.", page=1, col=RIGHT, y=(100, 200))]
    assert clean_mod.link_cross_page_continuations(blocks) == 1


# ---------------------------------------------------------------------------
# D07：段落中途的「40.」「f.」不是新列表項（01_extract.group_lines）
# ---------------------------------------------------------------------------

def _line(extract_mod, text, y, x1=291.0):
    return extract_mod.Line(text=text, bbox=pymupdf.Rect(45, y, x1, y + 10), size=9.0,
                            bold_all=False, first_bold=False, col=0)


def test_wrapped_number_stays_in_paragraph(extract_mod):
    lines = [_line(extract_mod, "The SYNTHETIC purple group demonstrates a broader range", 600),
             _line(extract_mod, "than the notation, particularly noticeable at L* values below", 611),
             _line(extract_mod, "40. Similar tendencies are observed for the brown group.", 622)]
    kinds = ["text", "text", "list_item"]
    groups = extract_mod.group_lines(lines, kinds, 9.0)
    assert len(groups) == 1 and groups[0][0] == "text"


def test_wrapped_letter_stays_in_paragraph(extract_mod):
    lines = [_line(extract_mod, "The SYNTHETIC vividness plane is segmented into categories", 380),
             _line(extract_mod, "into six SYNTHETIC categories, ranging in order from a to", 391),
             _line(extract_mod, "f. For instance, values less than 0.7 fall into plane a.", 402)]
    groups = extract_mod.group_lines(lines, ["text", "text", "list_item"], 9.0)
    assert len(groups) == 1


@pytest.mark.parametrize("prev_text, prev_x1", [
    ("SYNTHETIC steps for application are listed below:", 291.0),   # 冒號結尾：真正的列表
    ("SYNTHETIC first step ends here.", 291.0),                      # 句號結尾
    ("SYNTHETIC short line", 150.0),                                 # 短行（段落最後一行），不是自動換行
])
def test_real_list_items_still_split(extract_mod, prev_text, prev_x1):
    lines = [_line(extract_mod, "SYNTHETIC opening line of the paragraph that fills width", 600),
             _line(extract_mod, prev_text, 611, x1=prev_x1),
             _line(extract_mod, "2. Apply a SYNTHETIC thin layer of lip balm.", 622)]
    groups = extract_mod.group_lines(lines, ["text", "text", "list_item"], 9.0)
    assert len(groups) == 2 and groups[1][0] == "list_item"


def test_bullet_always_new_item(extract_mod):
    lines = [_line(extract_mod, "SYNTHETIC opening line of the paragraph that fills width", 600),
             _line(extract_mod, "SYNTHETIC second line of the paragraph that fills width", 611),
             _line(extract_mod, "• SYNTHETIC bullet item", 622)]
    groups = extract_mod.group_lines(lines, ["text", "text", "list_item"], 9.0)
    assert len(groups) == 2


# ---------------------------------------------------------------------------
# 真實文件（原檔不在這台電腦時略過）
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ar015_records(extract_mod, clean_mod, export_mod, tmp_path_factory):
    if AR015_PDF is None:
        pytest.skip("找不到 AR015 原始 PDF")
    doc = clean_mod.clean_document(extract_mod.extract_pdf(AR015_PDF, "AR015"))
    path, _ = export_mod.export(doc, SRC, tmp_path_factory.mktemp("ar015"))
    return [json.loads(s) for s in path.read_text(encoding="utf-8").splitlines()]


def test_ar015_d01_standard_deviation_sentence_complete(ar015_records):
    hits = [r for r in ar015_records if "4.99 for African American women, 4.03 for Hispanic" in r["display_text"]]
    assert len(hits) == 1 and hits[0]["pages"] == [5, 6]


def test_ar015_d02_african_american_rejoined(ar015_records):
    hits = [r for r in ar015_records if "Hispanic American, African American and Asian Chinese." in r["display_text"]]
    assert len(hits) == 1


@pytest.fixture(scope="module")
def ar012_cleaned(extract_mod, clean_mod):
    if AR012_PDF is None:
        pytest.skip("找不到 AR012 原始 PDF")
    return clean_mod.clean_document(extract_mod.extract_pdf(AR012_PDF, "AR012"))


def test_ar012_d07_no_mid_sentence_list_items(ar012_cleaned):
    starts = [b.cleaned_text[:12] for b in ar012_cleaned.blocks if b.block_type == "list_item"]
    assert not any(s.startswith("40. Similar") or s.startswith("f. For insta") for s in starts)
    joined = [b for b in ar012_cleaned.blocks if "L* values below 40. Similar tendencies" in " ".join(b.cleaned_text.split())]
    assert len(joined) == 1
