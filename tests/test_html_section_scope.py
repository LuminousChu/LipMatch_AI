"""HTML 章節範圍：網站通知、嵌入互動模組不得繼承文章章節；正文與文章標題照舊保留。

合成 HTML（SYNTHETIC）結構仿自 L'Oréal 美妝雜誌頁：文章標題在 articleHeader、正文在多個 rich-text、
中間嵌一個含按鈕的助理推廣模組，<main> 外有網站通知。最後一個測試讀真實 PR015 原 HTML（找不到就略過）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

SYNTHETIC_HTML = """<!doctype html>
<html><head><title>SYNTHETIC</title></head>
<body>
<div id="main-container">
  <main id="main" role="main"><div id="content"><div class="articleWrapper">
    <div class="articleHeader"><article class="info"><div class="content">
      <h1>How To Apply Matte Lipstick</h1>
      <div class="description">A little prep makes all the difference.</div>
    </div></article></div>
    <div class="rich-text"><div class="field-text"><p>Intro prose without any heading of its own, see <a href="#">this tip</a>.</p></div></div>
    <div class="beauty-guide-box"><div class="intro"><div class="header">Have questions about this article? Ask me!</div></div>
      <button type="button">Ask</button></div>
    <div class="rich-text"><div class="field-text">
      <h2>Do's</h2>
      <h3>1. Exfoliate first</h3>
      <p>Gently massage a lip scrub over your lips.</p>
      <h3>2. Choose the right shade</h3>
      <ul><li>Cool skin: soft pinks and rich plums.</li></ul>
    </div></div>
    <div class="product-card"><h3>Card heading</h3><p>Card text with a buy button.</p><button>Buy</button></div>
  </div></div></main>
  <div class="custom-notification"><div class="custom-notification-text"><p>This site is intended for US consumers.</p></div></div>
</div>
</body></html>
"""

NO_MAIN_HTML = """<!doctype html><html><body>
<div class="page"><h1>Title</h1><p>Body text.</p></div>
<div class="notice"><p>Site notice text.</p></div>
</body></html>
"""


def _clean(extract_mod, clean_mod, tmp_path, html, name="synthetic_scope.html"):
    p = tmp_path / name
    p.write_text(html, encoding="utf-8")
    return clean_mod.clean_document(extract_mod.extract_html(p, "PRX"))


def _by_text(doc, start):
    return next(b for b in doc.blocks if (b.cleaned_text or b.raw_text).strip().startswith(start))


def test_notice_outside_main_does_not_inherit(extract_mod, clean_mod, tmp_path):
    doc = _clean(extract_mod, clean_mod, tmp_path, SYNTHETIC_HTML)
    b = _by_text(doc, "This site is intended")
    assert b.section is None
    assert "section_not_inherited:outside_main_content" in b.cleaning_ops


def test_embedded_widget_with_control_does_not_inherit(extract_mod, clean_mod, tmp_path):
    doc = _clean(extract_mod, clean_mod, tmp_path, SYNTHETIC_HTML)
    b = _by_text(doc, "Have questions about this article")
    assert b.section is None
    assert "section_not_inherited:ui_module" in b.cleaning_ops


def test_normal_prose_keeps_article_title_and_subsection(extract_mod, clean_mod, tmp_path):
    doc = _clean(extract_mod, clean_mod, tmp_path, SYNTHETIC_HTML)
    assert _by_text(doc, "A little prep").section == "How To Apply Matte Lipstick"
    assert _by_text(doc, "Intro prose").section == "How To Apply Matte Lipstick"  # 同層容器、無互動元件
    assert _by_text(doc, "Gently massage").section == "How To Apply Matte Lipstick > Do's > 1. Exfoliate first"
    assert _by_text(doc, "Cool skin").section == "How To Apply Matte Lipstick > Do's > 2. Choose the right shade"
    for b in doc.blocks:
        assert not any(o.startswith("section_not_inherited") for o in b.cleaning_ops) or b.section is None


def test_heading_blocks_and_ids_unchanged(extract_mod, clean_mod, tmp_path):
    doc = _clean(extract_mod, clean_mod, tmp_path, SYNTHETIC_HTML)
    h = [b for b in doc.blocks if b.block_type == "heading"]
    assert [b.section for b in h][:4] == ["How To Apply Matte Lipstick", "How To Apply Matte Lipstick > Do's",
                                          "How To Apply Matte Lipstick > Do's > 1. Exfoliate first",
                                          "How To Apply Matte Lipstick > Do's > 2. Choose the right shade"]
    assert [b.block_id for b in doc.blocks] == [f"PRX_h{i + 1:04d}" for i in range(len(doc.blocks))]


def test_module_with_own_heading_is_not_treated_as_widget(extract_mod, clean_mod, tmp_path):
    """有自己標題的區塊（例如商品卡）不套 ui_module；它由自己的標題決定章節，是否為雜訊另由既有規則判斷。"""
    doc = _clean(extract_mod, clean_mod, tmp_path, SYNTHETIC_HTML)
    b = _by_text(doc, "Card text")
    assert not any(o.startswith("section_not_inherited") for o in b.cleaning_ops)


def test_without_main_landmark_rule_not_applied(extract_mod, clean_mod, tmp_path):
    doc = _clean(extract_mod, clean_mod, tmp_path, NO_MAIN_HTML, "no_main.html")
    assert _by_text(doc, "Site notice").section == "Title"
    assert _by_text(doc, "Body text").section == "Title"


def test_missing_source_keeps_old_behaviour(extract_mod, clean_mod, tmp_path):
    p = tmp_path / "gone.html"
    p.write_text(SYNTHETIC_HTML, encoding="utf-8")
    ext = extract_mod.extract_html(p, "PRX")
    p.unlink()
    doc = clean_mod.clean_document(ext)
    assert _by_text(doc, "This site is intended").section is not None
    assert any(i.issue_type == "section_scope_unchecked" for i in doc.issues)


TUTORIAL_CASES = {  # 2026-10-23 獨立複查重現的反例（SYNTHETIC）：正文旁或段內的操作按鈕不得清空章節
    "plain_body": '<div><p>Apply a thin layer of lipstick and let it set.</p></div>',
    "sibling_copy_control": '<div><p>Apply a thin layer of lipstick and let it set.</p><button type="button">Copy these steps</button></div>',
    "inline_copy_control": '<p>Apply a thin layer of lipstick and let it set. <button type="button">Copy</button></p>',
}


@pytest.mark.parametrize("case", list(TUTORIAL_CASES))
def test_tutorial_controls_keep_section(extract_mod, clean_mod, tmp_path, case):
    html = ('<!doctype html><html><body><main><article><h1>How to apply matte lipstick</h1>'
            + TUTORIAL_CASES[case] + '</article></main></body></html>')
    doc = _clean(extract_mod, clean_mod, tmp_path, html, f"{case}.html")
    b = _by_text(doc, "Apply a thin")
    assert not b.is_removed
    assert b.section == "How to apply matte lipstick"
    assert not any(o.startswith("section_not_inherited") for o in b.cleaning_ops)
    flagged = any(o.startswith("section_scope_review:") for o in b.cleaning_ops)
    assert flagged == (case != "plain_body")  # 有按鈕：保留章節但標記待核對
    assert flagged == any(i.issue_type == "section_scope_review" for i in doc.issues)


PR015 = Path(__file__).resolve().parents[1] / "data" / "raw" / "web" / "PR015_霧面唇彩搭配技巧_英文文獻.html"


@pytest.mark.skipif(not PR015.exists(), reason="找不到 PR015 原 HTML")
def test_pr015_widget_and_notice_blocked_normal_content_kept(extract_mod, clean_mod):
    doc = clean_mod.clean_document(extract_mod.extract_html(PR015, "PR015"))
    by = {b.block_id: b for b in doc.blocks}
    assert by["PR015_h0114"].section is None and "section_not_inherited:ui_module" in by["PR015_h0114"].cleaning_ops
    assert by["PR015_h0196"].section is None and "section_not_inherited:outside_main_content" in by["PR015_h0196"].cleaning_ops
    title = "How To Apply Matte Lipstick Like a Pro"
    assert by["PR015_h0119"].section == f"{title} > Matte Lipstick Do’s > 1. Always exfoliate first"
    assert by["PR015_h0123"].section == f"{title} > Matte Lipstick Do’s > 2. Choose the right shade"
    assert by["PR015_h0139"].section == f"{title} > Matte Lipstick Don’ts > 2. Don’t apply too many layers"
    assert by["PR015_h0108"].section == title
