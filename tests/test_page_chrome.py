"""網頁頁面框架文字：跳至內容、上一篇／下一篇、關閉鈕、圖片來源、作者欄標籤、隱私權連結、Next Up（SYNTHETIC）。

對應 2026-10-16 etl-meta-noise 之後抽查 rag_ready 仍留著的網頁非內容列：
- PR012／PR015／PR017「Go to content」「Photo courtesy of …」「Next Up: …」「Your Privacy Choices Notice at Collection」
- PR020／PR021「Jump to content」「Author Details」
- PR003／PR005「previous post」「next post」、語言提示框的關閉鈕「x」
所有測試文字都是合成的（SYNTHETIC），只仿照版面結構，不含任何真實文件內容。
"""

from __future__ import annotations

import json

import pytest

from lipmatch.sources import SourceInfo
from schemas import Document, SourceManifest, TextBlock
from tests.conftest import _load

SRC = SourceInfo(title="SYNTHETIC", authors=["Tester"], author_citation="Tester", year=2026, verified_from="synthetic")


# ---------------------------------------------------------------------------
# 純文字判斷
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Go to content", "html_meta:skip_link"),
    ("Jump to content", "html_meta:skip_link"),
    ("Skip to main content", "html_meta:skip_link"),
    ("previous post", "html_meta:post_nav"),
    ("  next post ", "html_meta:post_nav"),
    ("x", "html_meta:close_button"),
    ("×", "html_meta:close_button"),
    ("Author Details", "html_meta:author_label"),
    ("About the author", "html_meta:author_label"),
    ("Your Privacy Choices Notice at Collection", "html_meta:privacy_links"),
    ("Your Privacy Choices   Notice at Collection ", "html_meta:privacy_links"),
    ("Notice at Collection", "html_meta:privacy_links"),
    ("Photo courtesy of Synthetic Brand Paris", "html_meta:photo_credit"),
    ("Photo courtesy of Synth’Éa Paris", "html_meta:photo_credit"),
    ("Image credit: Test Studio", "html_meta:photo_credit"),
    ("Next Up:\xa0How To Apply Synthetic Makeup Like a Pro", "html_meta:next_up"),
    ("Next Up: Synthetic Lipstick Guide: Matte, Satin, Gloss, and More", "html_meta:next_up"),
])
def test_page_chrome_detected(clean_mod, text, expected):
    assert clean_mod.classify_html_page_chrome(text) == expected


@pytest.mark.parametrize("text", [
    "Go to content creators for synthetic shade inspiration.",          # 不是整段完全符合
    "The next post will cover synthetic liners.",
    "x 2 coats for a deeper synthetic finish",
    "Author details matter less than the synthetic shade itself.",
    "Your privacy choices are respected when you book a synthetic consult.",
    "Your Privacy Choices and synthetic shade picks",                    # 隱私標籤後接其他字
    "Photo courtesy of the tester, who wore 12 synthetic shades.",      # 來源不是人名／品牌名
    "Next Up: apply a synthetic balm before the lipstick.",              # 句號結尾，像正文
    "Warm undertones pair well with synthetic coral shades.",
    "Wear it with: Synthetic Talk Medium Lip Liner",                     # 商品搭配列：使用者 2026-10-16 決定保留
    "Next Up: " + "Synthetic Shade " * 8,                                 # 超過 PAGE_CHROME_MAX_LEN
    "Explain why",                                                     # 'x' 只在整段完全相同時才算
    "",
])
def test_content_not_page_chrome(clean_mod, text):
    assert clean_mod.classify_html_page_chrome(text) is None


@pytest.mark.parametrize("text,expected", [
    ("Your Privacy Choices", True), ("Notice at Collection Your Privacy Choices", True),
    ("Your Privacy Choices Matter", False), ("privacy", False), ("", False),
])
def test_privacy_link_row(clean_mod, text, expected):
    assert clean_mod.is_privacy_link_row(text) is expected


def test_page_chrome_with_data_signal_kept(clean_mod):
    # 形狀像圖片來源，但 raw 含色彩數據（L*）→ 不自動移除
    blk = TextBlock(doc_id="h", block_id="h_b000", block_type="paragraph", order=0,
                    raw_text="Next Up: Synthetic Shades at L* 45", cleaned_text="Next Up: Synthetic Shades at L* 45")
    assert clean_mod.classify_html_page_chrome(blk.cleaned_text) == "html_meta:next_up"
    clean_mod.mark_html_metadata([blk])
    assert not blk.is_removed


def test_page_chrome_heading_untouched(clean_mod):
    # 標題不在 METADATA_BLOCK_TYPES 內：形狀相同也不動
    blk = TextBlock(doc_id="h", block_id="h_b000", block_type="heading", heading_level=3, order=0,
                    raw_text="Author Details", cleaned_text="Author Details")
    clean_mod.mark_html_metadata([blk])
    assert not blk.is_removed


# ---------------------------------------------------------------------------
# 合成 HTML 端到端（擷取 → 清洗 → RAG 匯出）
# ---------------------------------------------------------------------------

SYNTHETIC_HTML = """<!doctype html>
<html><head><title>SYNTHETIC 頁面框架測試頁</title></head>
<body>
<div class="ContentJump"><p>Jump to content</p></div>
<main><article>
  <h1>Synthetic Shade Guide</h1>
  <p>Warm undertones pair well with synthetic coral shades.</p>
  <p>Next Up:&nbsp;How To Apply Synthetic Makeup Like a Pro</p>
  <p>Photo courtesy of Synthetic Brand Paris</p>
  <p>The next post will cover synthetic liners.</p>
  <p>Wear it with: Synthetic Talk Medium Lip Liner</p>
  <div class="AuthorBio"><p>Author Details</p><h3>Synthetic Tester</h3></div>
</article></main>
<section class="article-navi-footer"><p> previous post </p><p> next post </p></section>
<div class="languageNotice"><p>Synthetic language notice text.</p>   x </div>
<div class="custom-notification-text">Your Privacy Choices   Notice at Collection </div>
</body></html>
"""

CHROME_LINES = ["Jump to content", "Next Up: How To Apply Synthetic Makeup Like a Pro",
                "Photo courtesy of Synthetic Brand Paris", "Author Details", "previous post", "next post", "x",
                "Your Privacy Choices Notice at Collection"]
KEPT_LINES = ["Warm undertones pair well with synthetic coral shades.", "The next post will cover synthetic liners.",
              "Synthetic Tester", "Synthetic language notice text.",
              # 商品搭配列（PR020「Wear it with: … Lip Cheat」型）：使用者 2026-10-16 決定保留，唇線筆搭配屬妝容建議
              "Wear it with: Synthetic Talk Medium Lip Liner"]


@pytest.fixture(scope="module")
def export_mod():
    """載入 05_export_rag_ready.py（檔名以數字開頭，不能直接 import）。"""
    return _load("lipmatch_export", "05_export_rag_ready.py")


@pytest.fixture(scope="module")
def chrome_cleaned(extract_mod, clean_mod, tmp_path_factory):
    """把合成 HTML 寫到暫存檔，擷取並清洗。"""
    path = tmp_path_factory.mktemp("chrome_html") / "synthetic_chrome.html"
    path.write_text(SYNTHETIC_HTML, encoding="utf-8")
    return clean_mod.clean_document(extract_mod.extract_html(path, "SYN_CHROME"))


def _by_text(doc: Document) -> dict[str, TextBlock]:
    """{壓空白後的原文: 區塊}。"""
    return {" ".join(b.raw_text.split()): b for b in doc.blocks}


def test_page_chrome_removed(chrome_cleaned):
    blocks = _by_text(chrome_cleaned)
    for line in CHROME_LINES:
        assert line in blocks, f"擷取結果沒有 {line!r}（測試資料或擷取變了）"
        b = blocks[line]
        assert b.is_removed and b.removal_reason.startswith("html_meta:"), (line, b.removal_reason)


def test_page_content_kept(chrome_cleaned):
    blocks = _by_text(chrome_cleaned)
    for line in KEPT_LINES:
        assert line in blocks, line
        assert not blocks[line].is_removed, (line, blocks[line].removal_reason)


def test_page_chrome_not_in_rag_export(chrome_cleaned, export_mod, tmp_path):
    path, _ = export_mod.export(chrome_cleaned, SRC, tmp_path)
    texts = [json.loads(line)["display_text"] for line in path.read_text(encoding="utf-8").splitlines()]
    for line in CHROME_LINES:
        assert line not in texts, line
    assert "Warm undertones pair well with synthetic coral shades." in texts


def test_page_chrome_only_html(clean_mod):
    # PDF 不跑 mark_html_metadata：同樣的文字在 PDF 不會以 html_meta 移除
    m = SourceManifest(doc_id="p", source_path="x.pdf", source_type="pdf", sha256="0" * 64,
                       file_size_bytes=0, extractor="test")
    blk = TextBlock(doc_id="p", block_id="p_p0001_b000", block_type="paragraph", order=0, page_number=1,
                    raw_text="Author Details")
    doc = clean_mod.clean_document(Document(stage="extracted", manifest=m, blocks=[blk]))
    assert not (doc.blocks[0].removal_reason or "").startswith("html_meta:")
