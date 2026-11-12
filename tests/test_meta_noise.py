"""網頁署名／日期／閱讀時間列、教材 PDF 題組與殘片（SYNTHETIC）。

對應 2026-10-16 登錄 16 篇文獻後抽查 rag_ready 發現的雜訊：
- 網頁：PR024「By Maybelline New York • 六月 03, 2026」、PR021「8 minute read」、
  PR012 署名被拆成「By」「Gillian Fuller」「March 21, 2025」三塊。
- TX001（NCERT 職訓教材）：章末練習題、活動、學習目標、只剩半句的條列、正文中夾圖說、詞彙表多條併段。
所有測試文字都是合成的（SYNTHETIC），只仿照版面結構，不含任何真實文件內容。
"""

from __future__ import annotations

import json

import pytest

from lipmatch.eligibility import classify_blocks, exercise_block_ids
from lipmatch.sources import SourceInfo
from schemas import Document, SourceManifest, TextBlock
from tests.conftest import _load

SRC = SourceInfo(title="SYNTHETIC", authors=["Tester"], author_citation="Tester", year=2026, verified_from="synthetic")

# ---------------------------------------------------------------------------
# 網頁 metadata：純文字判斷
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("By Synthetic Brand Name • 六月 03, 2026", "html_meta:byline"),
    ("By Test Author", "html_meta:byline"),
    ("By", "html_meta:byline"),
    ("Written by J. Tester", "html_meta:byline"),
    ("March 21, 2025", "html_meta:date"),
    ("31st March 2026", "html_meta:date"),
    ("2026-06-03", "html_meta:date"),
    ("2026年6月3日", "html_meta:date"),
    ("Updated: May 1, 2025", "html_meta:date"),
    ("8 minute read", "html_meta:read_time"),
    ("14 min read", "html_meta:read_time"),
    ("閱讀時間 5 分鐘", "html_meta:read_time"),
])
def test_metadata_lines_detected(clean_mod, text, expected):
    assert clean_mod.classify_html_metadata(text) == expected


@pytest.mark.parametrize("text", [
    "By applying lip liner first, the colour lasts longer.",     # 'By' 開頭的正文句子
    "By the end of the day, matte formulas may feel dry.",
    "Warm undertones suit coral and brick red.",
    "In March 2025 the brand released a new shade range.",      # 句中提到日期
    "Apply for 8 minutes before blotting.",
    "By Test Author • a long note that is not a date",          # 分隔符後不是日期／閱讀時間
])
def test_content_lines_not_metadata(clean_mod, text):
    assert clean_mod.classify_html_metadata(text) is None


@pytest.mark.parametrize("text,expected", [
    ("Gillian Tester", True), ("Synthetic Brand New York", True), ("J. Tester", True), ("測試作者", True),
    ("the colour lasts longer", False), ("Apply gloss.", False), ("Shade 03", False), ("", False),
])
def test_person_name_line(clean_mod, text, expected):
    assert clean_mod.is_person_name_line(text) is expected


# ---------------------------------------------------------------------------
# 網頁 metadata：合成 HTML 端到端（擷取 → 清洗 → RAG 匯出）
# ---------------------------------------------------------------------------

SYNTHETIC_HTML = """<!doctype html>
<html><head><title>SYNTHETIC 署名測試頁</title></head>
<body><main><article>
  <h1>Synthetic Lip Guide</h1>
  <p>By</p>
  <p>Gillian Tester</p>
  <p>March 21, 2025</p>
  <ul><li>31st March 2026</li><li>By Synthetic Brand</li></ul>
  <p>14 minute read</p>
  <p>By Synthetic Brand New York • 六月 03, 2026</p>
  <h2>Choosing a shade</h2>
  <p>By applying a neutral liner first, the synthetic colour stays crisp.</p>
  <p>Synthetic Tester</p>
  <p>Warm undertones pair well with synthetic coral shades.</p>
</article></main></body></html>
"""

META_LINES = ["By", "Gillian Tester", "March 21, 2025", "31st March 2026", "By Synthetic Brand", "14 minute read",
              "By Synthetic Brand New York • 六月 03, 2026"]


@pytest.fixture(scope="module")
def export_mod():
    """載入 05_export_rag_ready.py（檔名以數字開頭，不能直接 import）。"""
    return _load("lipmatch_export", "05_export_rag_ready.py")


@pytest.fixture(scope="module")
def meta_cleaned(extract_mod, clean_mod, tmp_path_factory):
    """把合成 HTML 寫到暫存檔，擷取並清洗。"""
    path = tmp_path_factory.mktemp("meta_html") / "synthetic_meta.html"
    path.write_text(SYNTHETIC_HTML, encoding="utf-8")
    return clean_mod.clean_document(extract_mod.extract_html(path, "SYN_META"))


def _by_text(doc: Document) -> dict[str, TextBlock]:
    """{壓空白後的原文: 區塊}。"""
    return {" ".join(b.raw_text.split()): b for b in doc.blocks}


def test_byline_date_read_time_removed(meta_cleaned):
    blocks = _by_text(meta_cleaned)
    for line in META_LINES:
        assert line in blocks, f"擷取結果沒有 {line!r}（測試資料或擷取變了）"
        b = blocks[line]
        assert b.is_removed and b.removal_reason.startswith("html_meta:"), (line, b.removal_reason)
    assert blocks["Gillian Tester"].removal_reason == "html_meta:byline_author"
    assert blocks["14 minute read"].removal_reason == "html_meta:read_time"


def test_article_content_kept(meta_cleaned):
    blocks = _by_text(meta_cleaned)
    for line in ("By applying a neutral liner first, the synthetic colour stays crisp.",
                 "Warm undertones pair well with synthetic coral shades."):
        assert not blocks[line].is_removed, line
    # 人名形狀的短行只有緊接在單獨 'By' 後面才移除；其他位置不動
    assert not blocks["Synthetic Tester"].is_removed


def test_metadata_not_in_rag_export(meta_cleaned, export_mod, tmp_path):
    path, _ = export_mod.export(meta_cleaned, SRC, tmp_path)
    texts = [json.loads(line)["display_text"] for line in path.read_text(encoding="utf-8").splitlines()]
    for line in META_LINES:
        assert line not in texts, line
    assert "Warm undertones pair well with synthetic coral shades." in texts


def test_metadata_with_data_signal_kept(clean_mod):
    # 含色彩數據的區塊即使形狀像 metadata 也不自動移除（與 HTML 雜訊同一道保險）
    m = SourceManifest(doc_id="h", source_path="x", source_type="html", sha256="0" * 64,
                       file_size_bytes=0, extractor="test")
    blk = TextBlock(doc_id="h", block_id="h_b000", block_type="paragraph", order=0,
                    raw_text="By Lab Tester • 2026-06-03", cleaned_text="By Lab Tester • 2026-06-03")
    blk_data = TextBlock(doc_id="h", block_id="h_b001", block_type="paragraph", order=1,
                         raw_text="2026-06-03", cleaned_text="2026-06-03")
    clean_mod.mark_html_metadata([blk, blk_data])
    assert blk.is_removed
    blk_data.raw_text = "2026-06-03 L* 45"     # raw 含 L* → DATA_SIGNAL_RE
    blk_data.is_removed, blk_data.removal_reason = False, None
    clean_mod.mark_html_metadata([blk_data])
    assert not blk_data.is_removed


# ---------------------------------------------------------------------------
# 教材 PDF：合成區塊（不經 01 擷取，直接組 Document 跑 02 清洗與 RAG 篩選）
# ---------------------------------------------------------------------------

REVIEW = "SYNTHETIC: 圖表區文字，請人工確認"


def _pdf_doc(specs: list[tuple]) -> Document:
    """specs: [(block_type, page, text)]；figure_text 一律標成待確認（仿 01 擷取的行為）。"""
    m = SourceManifest(doc_id="SYN_TX", source_path="x", source_type="pdf", sha256="0" * 64,
                       file_size_bytes=0, extractor="test", pages_processed=sorted({s[1] for s in specs}))
    blocks = []
    for i, (bt, page, text) in enumerate(specs):
        review = bt == "figure_text"
        blocks.append(TextBlock(doc_id="SYN_TX", block_id=f"SYN_TX_b{i:03d}", block_type=bt, order=i,
                                page_number=page, raw_text=text, heading_level=1 if bt == "heading" else None,
                                needs_review=review, review_reasons=[REVIEW] if review else []))
    return Document(stage="extracted", manifest=m, blocks=blocks)


# 仿 TX001 版面：每頁左欄 'Notes'、節末練習題與學習目標、下一節沒有標題直接接正文、書末詞彙表與答案
TEXTBOOK = [
    ("heading", 1, "Session 1: Synthetic Lip Basics"),                                        # 0
    ("paragraph", 1, "A synthetic lip primer smooths the surface before colour."),             # 1 正文
    ("list_item", 1, "• • Apply the synthetic balm and wait for it to"),                       # 2 前半句
    ("figure_text", 1, "absorb fully before applying colour."),                                # 3 後半在 figure_text
    ("paragraph", 1, "4. Flower style synthetic design Figure 1.35: Synthetic caption"),       # 4 正文夾圖說
    ("paragraph", 1, "Figure 1.36: Synthetic standalone caption"),                             # 5 圖說被當正文
    ("heading", 2, "Notes"),                                                                   # 6 頁邊欄標籤
    ("figure_text", 2, "Check Your Progress"),                                                 # 7 題組開始
    ("label", 2, "A. Multiple Choice Questions"),                                              # 8
    ("list_item", 2, "1. Which synthetic shade suits a warm undertone?"),                      # 9 題目
    ("heading", 3, "Notes"),                                                                   # 10 跨頁仍在題組
    ("list_item", 3, "4. Synthetic gloss should always be applied twice."),                    # 11 是非題
    ("list_item", 3, "2. __________ is used to define the synthetic lip line."),               # 12 填空
    ("figure_text", 3, "What have you learned?"),                                              # 13 學習目標
    ("label", 3, "After completing this session, you will be able to:"),                       # 14
    ("list_item", 3, "• Explain the synthetic primer steps."),                                 # 15 目標條列
    ("list_item", 3, "• • Synthetic toners without alcohol help against moisture loss."),      # 16 下一節正文
    ("heading", 4, "Notes"),                                                                   # 17
    ("figure_text", 4, "Glossary"),                                                            # 18 詞彙表
    ("paragraph", 4, "Synthetic tint: A colour mixed with white. Synthetic shade: A colour mixed with black."),
    ("paragraph", 4, "Synthetic tone: A colour mixed with grey."),                             # 20 單一詞條
    ("figure_text", 5, "Answer Key"),                                                          # 21 答案
    ("label", 5, "Unit I: Synthetic Unit"),                                                    # 22
    ("paragraph", 5, "C. State whether the following statements are true or false :"),        # 23
    ("paragraph", 5, "1. True 2. False 3. True"),                                              # 24
    ("heading", 5, "Notes"),                     # 25–26：讓 'Notes' 出現在 >= 5 頁，才會被認成頁邊欄標籤
    ("heading", 6, "Notes"),
]


@pytest.fixture(scope="module")
def tx_cleaned(clean_mod):
    """仿 TX001 版面的合成教材，跑完 02 清洗。"""
    return clean_mod.clean_document(_pdf_doc(TEXTBOOK))


def _clean_reasons(b: TextBlock) -> list[str]:
    """02 清洗新增的待確認原因（以 'clean:' 開頭）。"""
    return [r for r in b.review_reasons if r.startswith("clean:")]


def test_exercise_region_excluded(tx_cleaned):
    r = classify_blocks(tx_cleaned)
    ids = [b.block_id for b in tx_cleaned.blocks]
    for i in (9, 11, 12, 15, 24):          # 題目、是非題、填空、學習目標、答案
        assert r[ids[i]] == "exercise", (i, tx_cleaned.blocks[i].cleaned_text, r[ids[i]])


def test_content_around_exercises_kept(tx_cleaned):
    r = classify_blocks(tx_cleaned)
    ids = [b.block_id for b in tx_cleaned.blocks]
    assert r[ids[1]] is None
    # 學習目標後面沒有標題、直接接下一節正文（雙項目符號）：不能被當成題組
    assert r[ids[16]] is None
    # 詞彙表是知識內容：單一詞條保留
    assert r[ids[20]] is None


def test_exercise_markers_only_textbook():
    # 一般論文段落提到 activity、question 不觸發題組（標記必須單獨成一行）
    m = SourceManifest(doc_id="p", source_path="x", source_type="pdf", sha256="0" * 64, file_size_bytes=0,
                       extractor="test")
    b = TextBlock(doc_id="p", block_id="p_b000", block_type="paragraph", order=0, page_number=1,
                  raw_text="Activity 1 of the study asked: which shade is preferred?",
                  cleaned_text="Activity 1 of the study asked: which shade is preferred?",
                  search_text="Activity 1 of the study asked: which shade is preferred?")
    assert exercise_block_ids(Document(stage="cleaned", manifest=m, blocks=[b])) == set()


def test_truncated_bullet_flagged_not_deleted(tx_cleaned):
    b = tx_cleaned.blocks[2]
    assert not b.is_removed and b.cleaned_text == "• • Apply the synthetic balm and wait for it to"
    assert b.needs_review and any("只剩前半句" in x for x in _clean_reasons(b))
    assert classify_blocks(tx_cleaned)[b.block_id] == "needs_review"


def test_embedded_caption_flagged(tx_cleaned):
    b = tx_cleaned.blocks[4]
    assert b.block_type == "paragraph" and not b.is_removed
    assert any("Figure 1.35" in x for x in _clean_reasons(b))


def test_standalone_caption_retyped(tx_cleaned):
    b = tx_cleaned.blocks[5]
    assert b.block_type == "figure_caption" and "retyped_as_figure_caption" in b.cleaning_ops
    assert classify_blocks(tx_cleaned)[b.block_id] is None


def test_merged_glossary_flagged(tx_cleaned):
    b = tx_cleaned.blocks[19]
    assert any("詞彙表" in x for x in _clean_reasons(b))
    assert not _clean_reasons(tx_cleaned.blocks[20])


def test_clean_is_idempotent(clean_mod, tx_cleaned):
    again = clean_mod.clean_document(tx_cleaned)
    assert [(b.block_type, b.review_reasons, b.is_removed) for b in again.blocks] == \
        [(b.block_type, b.review_reasons, b.is_removed) for b in tx_cleaned.blocks]


def test_no_dangling_continuation_after_flag(clean_mod, export_mod, tmp_path):
    # 前半句被標待確認後，若有續段接在它後面，續段也要一起標記，匯出不可出現只剩半句的紀錄
    doc = _pdf_doc([
        ("paragraph", 1, "The synthetic primer is applied and then"),
        ("figure_text", 1, "left to set."),
        ("paragraph", 2, "pressed gently with a synthetic sponge."),
    ])
    cleaned = clean_mod.clean_document(doc)
    path, _ = export_mod.export(cleaned, SRC, tmp_path)
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    exported = {i for r in records for i in r["block_ids"]}
    assert all(not r["continues_from_block_id"] or r["continues_from_block_id"] in exported for r in records)
    assert cleaned.blocks[0].needs_review


def test_table_row_ending_with_number_not_flagged(clean_mod):
    # 表格殘片（結尾是數字、下一塊是 'a 3.1' 顯著性標記）不是句子，維持原判斷（AR001 不受影響）
    doc = _pdf_doc([
        ("paragraph", 1, "Synthetic device 47.360 a 3.366 20.024 b 2.856"),
        ("figure_text", 1, "a 3.171 18.608 c 2.278"),
    ])
    cleaned = clean_mod.clean_document(doc)
    assert not _clean_reasons(cleaned.blocks[0])
