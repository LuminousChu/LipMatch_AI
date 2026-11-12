"""TX001 教材抽取修復（2026-10-17）：浮水印座標誤差、版面裝飾被當圖表、窄面板正文認不出來。

對應問題（詳見 docs/mvp/handoffs/ETL-TX001.md）：
1. 整頁底圖浮水印在 get_image_info 是未裁切座標、在 get_text 是裁到頁內的座標，比對失敗，
   整頁被當成圖片區，所有短行都變 figure_text。
2. 底圖拿掉後，外框線、浮水印底塊、面板底色被併成一個大叢集，被誤判成圖表。
3. 面板寬約 320pt、每行 55–65 字，達不到「> 60 字長行占 75%」的底色框門檻。

反例同樣重要：真正的長條圖、折線圖、線框表格、數字表格、小字引用框都不能因此被放行成正文。
合成資料標示 SYNTHETIC（tests/fixtures/synthetic_layout_pdf.py），不使用真實全文。
"""
from __future__ import annotations

import pymupdf
import pytest

from tests.fixtures.synthetic_layout_pdf import PAGE_H, PAGE_W, PANEL_STEPS, WATERMARK_RECT

R = pymupdf.Rect
P = pymupdf.Point
PAGE = R(0, 0, PAGE_W, PAGE_H)


@pytest.fixture(scope="module")
def layout_doc(extract_mod, tmp_path_factory):
    """三頁合成教材的擷取結果。"""
    from tests.fixtures.synthetic_layout_pdf import build_layout_pdf
    pdf = build_layout_pdf(tmp_path_factory.mktemp("tx001_layout") / "synthetic_layout.pdf")
    return extract_mod.extract_pdf(pdf, "SYN_LAYOUT")


def _blocks(doc, page: int, block_type: str) -> list:
    """取出某頁某型別的區塊。"""
    return [b for b in doc.blocks if b.page_number == page and b.block_type == block_type]


def _line(extract_mod, text: str, x0: float, y0: float, x1: float, size: float = 10.0):
    """建立一條 Line（高 12pt）；只填判斷會用到的欄位。"""
    return extract_mod.Line(text=text, bbox=R(x0, y0, x1, y0 + 12), size=size, bold_all=False, first_bold=False)


def _rule(x0: float, y0: float, x1: float, y1: float) -> dict:
    """get_drawings 格式的單條直線（無填色）。"""
    return {"rect": R(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)),
            "items": [("l", P(x0, y0), P(x1, y1))], "fill": None}


def _fill_rect(r: pymupdf.Rect, fill=(0.9, 0.8, 0.85)) -> dict:
    """get_drawings 格式的純矩形填色。"""
    return {"rect": R(r), "items": [("re", R(r), 1)], "fill": fill}


# ---------------------------------------------------------------------------
# 整份合成 PDF：修好的與不能被放行的
# ---------------------------------------------------------------------------

def test_bleeding_watermark_is_not_image_region(layout_doc):
    """超出頁面的整頁底圖仍被認成浮水印：每頁都不建立圖片區域，manifest 有紀錄。"""
    assert not [b for b in layout_doc.blocks if b.block_type == "image_region"]
    assert "watermark_images_excluded" in layout_doc.manifest.metadata


def test_panel_body_is_not_figure_text(layout_doc):
    """說明面板裡的步驟是正文：不可變成 figure_text，每一步都要完整出現在段落裡。"""
    assert not _blocks(layout_doc, 1, "figure_text")
    assert not _blocks(layout_doc, 1, "chart_region")
    paragraphs = "\n".join(b.raw_text for b in _blocks(layout_doc, 1, "paragraph"))
    for step in PANEL_STEPS:
        assert step in paragraphs


def test_bar_chart_still_chart(layout_doc):
    """長條圖只由直線＋純矩形組成（與面板相同），但框內是短標籤：仍是圖表，標籤仍是圖表文字。"""
    assert len(_blocks(layout_doc, 2, "chart_region")) == 1
    fig = "\n".join(b.raw_text for b in _blocks(layout_doc, 2, "figure_text"))
    for label in ("Red", "Coral", "60"):
        assert label in fig
    assert not any("Coral" in b.raw_text for b in _blocks(layout_doc, 2, "paragraph"))


def test_line_chart_and_ruled_table_stay_figure(layout_doc):
    """折線圖（斜線）與分隔線圍成的兩欄對照表都不可當正文：保留圖區，交人工確認。"""
    assert len(_blocks(layout_doc, 3, "chart_region")) == 2
    fig = "\n".join(b.raw_text for b in _blocks(layout_doc, 3, "figure_text"))
    assert "2019" in fig
    assert "Establishing the nature of the call" in fig


# ---------------------------------------------------------------------------
# 第 1 點：浮水印比對
# ---------------------------------------------------------------------------

def test_matches_watermark_unclipped_and_clipped(extract_mod):
    """原始框（超出頁面）和裁到頁內的框都要對得上；不相干的圖片對不上。"""
    wm = [R(WATERMARK_RECT)]
    assert extract_mod.matches_watermark(R(WATERMARK_RECT), wm, PAGE)
    assert extract_mod.matches_watermark(R(0, 0, PAGE_W, PAGE_H), wm, PAGE)
    assert not extract_mod.matches_watermark(R(400, 200, 540, 300), wm, PAGE)


def test_old_exact_comparison_would_miss(extract_mod):
    """回歸保護：只比原始框（舊做法）時，裁到頁內的框誤差約 95pt，比不上。"""
    assert not extract_mod.same_box(R(0, 0, PAGE_W, PAGE_H), R(WATERMARK_RECT))


# ---------------------------------------------------------------------------
# 第 2 點：版面裝飾
# ---------------------------------------------------------------------------

def test_layout_decoration_reasons(extract_mod):
    """A 整頁／出血外框、B 浮水印底塊、C 文字底色框；長條圖的長條不是裝飾。"""
    wm = [R(WATERMARK_RECT)]
    body = [_line(extract_mod, "Body text inside a tinted box that is long enough here.", 70, 110 + 14 * i, 370)
            for i in range(3)]
    frame = {"rect": R(-5, -5, PAGE_W + 5, PAGE_H + 5), "items": [("re", R(-5, -5, PAGE_W + 5, PAGE_H + 5), 1)],
             "fill": None}
    assert extract_mod.layout_decoration(frame, PAGE, wm, []) == "A"
    assert extract_mod.layout_decoration(_rule(-3, 400, 300, 400), PAGE, wm, []) == "A"
    assert extract_mod.layout_decoration(_fill_rect(R(0, 0, PAGE_W, PAGE_H), (0, 0, 0)), PAGE, wm, []) == "A"
    assert extract_mod.layout_decoration(_fill_rect(R(2, 2, PAGE_W - 2, 500)), PAGE, wm[:0], []) is None
    small_wm = [R(100, 100, 300, 200)]
    assert extract_mod.layout_decoration(_fill_rect(R(100, 100, 300, 200)), PAGE, small_wm, []) == "B"
    assert extract_mod.layout_decoration(_fill_rect(R(61, 100, 385, 160)), PAGE, [], body) == "C"
    assert extract_mod.layout_decoration(_fill_rect(R(150, 200, 190, 320)), PAGE, [], body) is None


def test_chart_regions_with_only_decorations(extract_mod, tmp_path):
    """排除裝飾後沒有繪圖：不叢集、不產生圖表區域。"""
    doc = pymupdf.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    page.draw_rect(R(-5, -5, PAGE_W + 5, PAGE_H + 5), color=(0, 0, 0), width=2)
    assert extract_mod.chart_regions(page, [], []) == []
    drawings = extract_mod.content_drawings(page, [], [])
    assert drawings == []
    doc.close()


# ---------------------------------------------------------------------------
# 第 3 點：窄面板正文 vs. 圖表／表格
# ---------------------------------------------------------------------------

def test_is_prose_line(extract_mod):
    """文句行：夠長、字母為主、字級接近正文。數字列、短標籤、小字都不是。"""
    assert extract_mod.is_prose_line(_line(extract_mod, "Before applying lipstick, outline the lip first.", 0, 0, 220), 10)
    assert not extract_mod.is_prose_line(_line(extract_mod, "2,500 K 0.19 0.51 0.13 0.65 0.26 0.35", 0, 0, 225), 10)
    assert not extract_mod.is_prose_line(_line(extract_mod, "Yellow-Green", 0, 0, 75), 10)
    assert not extract_mod.is_prose_line(
        _line(extract_mod, "Development of a lip colour chart across four", 0, 0, 186, size=8.5), 10)


def test_text_box_narrow_panel_needs_body_size(extract_mod):
    """每行 50–60 字、寫滿 320pt 框寬：有 body_size 才算正文框；不給 body_size 維持舊判斷。"""
    r = R(61, 100, 385, 200)
    lines = [_line(extract_mod, t, 65, 105 + 14 * i, 375) for i, t in enumerate([
        "Use a suitable foundation which suits your skin tone well.",
        "Use light shades to cover up dark circles on the forehead.",
        "Before applying lipstick, outline the lips first neatly.",
        "Make-up for the workplace should look natural and clean.",
    ])]
    assert extract_mod.is_text_box(r, lines, body_size=10)
    assert not extract_mod.is_text_box(r, lines)


def test_text_box_rejects_numeric_wide_rows(extract_mod):
    """數字表格列也寫滿框寬（TH014 色差表、AR002 相關係數表），不可因寬度規則被當成正文框。"""
    r = R(99, 90, 497, 300)
    lines = [_line(extract_mod, f"{i}  (115, 82, 68) (37.8, 12.4, 14.6) (30.2, 13.4, 12.8) 7.6", 100, 100 + 14 * i, 470)
             for i in range(1, 8)]
    assert not extract_mod.is_text_box(r, lines, body_size=10, drawings=[_rule(99, 95, 497, 95)])


def test_ruled_prose_panel_accepts_bullet_panel(extract_mod):
    """分隔線＋色塊標題列圍成、框內是條列正文（TX001 p95 職場儀容）→ 正文面板。

    條列每行只有 30–40 字、行寬不到框寬 70%，寬度規則抓不到，只能靠分隔線面板規則。"""
    r = R(211, 69, 535, 186)
    lines = [_line(extract_mod, "Hands and Feet Care", 320, 72, 424)] + [
        _line(extract_mod, t, 215, 90 + 14 * i, 215 + 4.8 * len(t)) for i, t in enumerate([
            "• Brush teeth twice a day.",
            "• Nails should be well clipped.",
            "• Rinse your mouth after every meal.",
            "• Avoid chewing gums and tobacco.",
        ])]
    assert all(ln.bbox.width < extract_mod.PROSE_WIDE_SHARE * r.width for ln in lines)
    drawings = [_fill_rect(R(211, 69, 535, 86), (0.71, 0.08, 0.43)), _rule(211, 69, 211, 186),
                _rule(535, 69, 535, 186), _rule(211, 186, 535, 186)]
    assert extract_mod.is_ruled_prose_panel(r, lines, drawings, 10)
    assert extract_mod.is_text_box(r, lines, body_size=10, drawings=drawings)


def test_ruled_prose_panel_rejects_side_by_side_table(extract_mod):
    """左右並排 >= 2 列（兩欄對照表）→ 不是正文面板，即使每格都是英文句子。"""
    r = R(61, 370, 534, 470)
    lines = []
    for i, (a, b) in enumerate([("Open questions", "Establishing the nature of the call"),
                                ("Closed questions", "To establish or confirm information"),
                                ("Probing questions", "Gathering specific details of needs")]):
        lines += [_line(extract_mod, a, 65, 375 + 22 * i, 160), _line(extract_mod, b, 183, 375 + 22 * i, 360)]
    drawings = [_rule(61, 372 + 22 * i, 534, 372 + 22 * i) for i in range(4)] + [_rule(180, 372, 180, 438)]
    assert not extract_mod.is_ruled_prose_panel(r, lines, drawings, 10)


def test_ruled_prose_panel_rejects_curves_and_short_labels(extract_mod):
    """有曲線（色輪、圓餅圖）或框內是短標籤（長條圖）→ 不是正文面板。"""
    r = R(61, 100, 385, 300)
    prose = [_line(extract_mod, "Apply the lightest powder first over the entire eyelid.", 65, 105 + 14 * i, 330)
             for i in range(4)]
    curve = {"rect": R(100, 150, 200, 250), "items": [("c", P(100, 200), P(120, 150), P(180, 150), P(200, 200))],
             "fill": (0.9, 0.1, 0.1)}
    assert not extract_mod.is_ruled_prose_panel(r, prose, [_rule(61, 100, 385, 100), curve], 10)
    labels = [_line(extract_mod, t, 70 + 60 * i, 280, 100 + 60 * i) for i, t in enumerate(["Red", "Pink", "Nude", "Coral"])]
    bars = [_fill_rect(R(70 + 60 * i, 200, 100 + 60 * i, 275), (0.8, 0.2, 0.3)) for i in range(4)]
    assert not extract_mod.is_ruled_prose_panel(r, labels, bars + [_rule(65, 276, 380, 276)], 10)


# ---------------------------------------------------------------------------
# 連帶問題：修好後冒出來的兩行頁尾（02_clean）
# ---------------------------------------------------------------------------

def _footer_page(page: int, footer: str, body_y1: float = 690.0) -> list:
    """一頁：正文一段＋「章名 ⏎ 頁碼」兩行頁尾（高 21pt，在頁面 90% 處、底部 7% 以外）。"""
    from schemas import TextBlock
    mk = lambda i, text, bbox: TextBlock(doc_id="t", block_id=f"t_p{page:04d}_b{i:03d}", block_type="paragraph",
                                         order=page * 10 + i, page_number=page, bbox=bbox, section="SYNTHETIC",
                                         raw_text=text, cleaned_text=text, search_text=text)
    return [mk(0, f"SYNTHETIC body text on page {page} that is long and different from others.", (61, 80, 535, body_y1)),
            mk(1, footer, (330, 708, 535, 729))]


def test_two_line_running_footer_removed(clean_mod):
    """「Salon Reception Duties ⏎ 57」在 >= 3 頁同高度重複 → 頁尾，移除。"""
    blocks = sum((_footer_page(p, f"SYNTHETIC Reception Duties\n{p + 1}") for p in range(68, 76, 2)), [])
    removed = clean_mod.detect_running_heads_outside_margin(blocks, 3)
    assert {b.block_id for b in blocks if b.bbox[1] == 708} == set(removed)
    assert all("page_footer" in r for r in removed.values())


def test_two_line_running_head_shape_guards(clean_mod):
    """兩行但沒有一行是純頁碼（一般兩行段落）、三行、兩行都是頁碼 → 都不是書眉外形。"""
    assert clean_mod.is_running_head_shape("SYNTHETIC Reception Duties\n57", 21)
    assert not clean_mod.is_running_head_shape("Apply the lipstick evenly\nthen blot with a tissue.", 21)
    assert not clean_mod.is_running_head_shape("Chapter\n57\nNotes", 30)
    assert not clean_mod.is_running_head_shape("56\n57", 21)
    assert not clean_mod.is_running_head_shape("SYNTHETIC Reception Duties\n57", 45)


def test_two_line_footer_on_two_pages_kept(clean_mod):
    """只出現兩頁 → 不夠判斷是頁尾，保留。"""
    blocks = sum((_footer_page(p, f"SYNTHETIC Reception Duties\n{p + 1}") for p in (68, 70)), [])
    assert clean_mod.detect_running_heads_outside_margin(blocks, 3) == {}
