"""HTML 自訂元素（Vue／Web Components）的擷取與清洗。

測試資料全部是合成 HTML（SYNTHETIC），結構仿自：
- 媚比琳台灣：整篇文章包在 <article-page-wrapper>，結尾有 div.bestsellers 暢銷商品區。
- L'Oréal：文末用 <oap-slider> 輪播其他文章卡片。
不含任何真實網頁內容。
"""

from __future__ import annotations

import pytest

SYNTHETIC_HTML = """<!doctype html>
<html><head><title>SYNTHETIC 測試頁</title></head>
<body>
<main>
  <article-page-wrapper>
    <h1>如何畫出漸層唇</h1>
    <p>這是一段合成的導言文字，用來確認段落會被拆開。</p>
    <ol>
      <li><div class="how-to-step"><h2>步驟 1：先畫外圈</h2>
          <div class="how-to-step__description">用較深的顏色勾勒唇線。</div></div></li>
      <li><div class="how-to-step"><h2>步驟 2：再填中央</h2>
          <div class="how-to-step__description">用較淺的顏色填滿中央並暈開。</div></div></li>
    </ol>
    <p>行內自訂元素 <oap-favorite>收藏</oap-favorite> 應該留在同一段。</p>
    <div class="bestsellers">
      <div class="bestsellers__heading"><h2>完美造型</h2></div>
      <div class="bestsellers__cta">瀏覽全部產品</div>
    </div>
  </article-page-wrapper>
  <oap-slider id="slider-1">
    <ul><li><article class="oap-card"><h3>Unrelated Hair Dye Article</h3></article></li></ul>
  </oap-slider>
  <article-share-item><p>Share on Pinterest</p></article-share-item>
</main>
</body></html>
"""


@pytest.fixture(scope="module")
def custom_extracted(extract_mod, tmp_path_factory):
    """把合成 HTML 寫到暫存檔後擷取，回傳 Document(stage='extracted')。"""
    path = tmp_path_factory.mktemp("custom_html") / "synthetic_custom.html"
    path.write_text(SYNTHETIC_HTML, encoding="utf-8")
    return extract_mod.extract_html(path, "SYN_CUSTOM")


@pytest.fixture(scope="module")
def custom_cleaned(clean_mod, custom_extracted):
    """清洗後的 Document。"""
    return clean_mod.clean_document(custom_extracted)


def _texts(doc, *, kept_only: bool = False) -> list[str]:
    """取出各區塊壓空白後的文字；kept_only=True 時只取清洗後保留的區塊。"""
    return [" ".join((b.cleaned_text if kept_only else b.raw_text).split())
            for b in doc.blocks if not (kept_only and b.is_removed)]


def test_custom_wrapper_split_into_headings_and_paragraphs(custom_extracted):
    texts = _texts(custom_extracted)
    # 修正前整篇會黏成一個 paragraph；修正後每個標題、段落各自成塊
    assert "如何畫出漸層唇" in texts
    assert "步驟 1：先畫外圈" in texts
    assert "用較深的顏色勾勒唇線。" in texts
    assert "步驟 2：再填中央" in texts
    headings = [b.raw_text.strip() for b in custom_extracted.blocks if b.block_type == "heading"]
    assert "步驟 2：再填中央" in headings


def test_inline_custom_element_stays_in_paragraph(custom_extracted):
    # 沒有區塊子元素的自訂元素仍當行內處理，不拆段
    assert "行內自訂元素 收藏 應該留在同一段。" in _texts(custom_extracted)


def test_noise_custom_elements_and_bestsellers_removed(custom_cleaned):
    kept = _texts(custom_cleaned, kept_only=True)
    for noise in ("完美造型", "瀏覽全部產品", "Unrelated Hair Dye Article", "Share on Pinterest"):
        assert noise not in kept, noise
    reasons = {b.removal_reason for b in custom_cleaned.blocks if b.is_removed}
    assert "html_noise:custom_tag=oap-slider" in reasons
    assert "html_noise:custom_tag=article-share-item" in reasons


def test_article_content_kept_after_cleaning(custom_cleaned):
    kept = _texts(custom_cleaned, kept_only=True)
    for must in ("如何畫出漸層唇", "步驟 1：先畫外圈", "用較淺的顏色填滿中央並暈開。"):
        assert must in kept, must
