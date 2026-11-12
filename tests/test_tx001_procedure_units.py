"""TX001 粉底教學匯出缺口（2026-10-22，M02）：標題框表格、表格旁圖說、實作活動與測驗的區分。

對應問題（docs/mvp/2026-10-22_G1_M02遮蓋資料查核_已有教學未取回與TX001匯出缺口.md）：
1. p.26「Application of Foundation」11 步表格被判 partial：右邊圖說第一行「Figure 1.14: Application of」
   和表格第 2 步同高被併成一行、當成表格內文字丟掉，只剩「cream foundation」落在表格旁 → 疑似殘片 → partial。
   同時粉底框和下方「Application of Concealer」框被認成同一張表，且「標題列 + 編號步驟」被當成沒有表頭。
2. p.28–29 Activity 1 重複同一套粉底步驟，因整個練習區排除而沒匯出；測驗（Check Your Progress）必須仍排除。

合成資料標示 SYNTHETIC；真實 PDF 測試在檔案不存在時略過。程式資料夾沒有 data/raw 時，
可設環境變數 LIPMATCH_RAW_DIR 指向原始檔所在的 data/raw。
"""
from __future__ import annotations

import os
from pathlib import Path

import pymupdf
import pytest

from lipmatch.eligibility import classify_blocks, practical_activities
from schemas import Document, SourceManifest, TextBlock
from tests.conftest import ROOT, _load

_RAW_DIRS = [ROOT / "data" / "raw"] + ([Path(os.environ["LIPMATCH_RAW_DIR"])] if os.environ.get("LIPMATCH_RAW_DIR") else [])
TX001_PDF = next((p for d in _RAW_DIRS if d.exists() for p in sorted(d.rglob("TX001_*.pdf"))), None)

FOUNDATION_STEPS = [
    "Decant foundation from the container onto a palette.",
    "Match the foundation with the client’s skin tone using a damp sponge or fingers.",
    "Cover the eyelids and lips.",
    "Apply green cover stick/concealer before the foundation if you have any pimples or red areas.",
    "Squeeze out some foundation onto your spatula and apply it to your hand.",
    "If you are using your hands to apply foundations ,then spread a dot on the forehead, cheeks, nose, chin, and jawline .",
    "Take your foundation brush and blend the foundation in downward stroking movements (Figure 1.14).",
    "Ensure that no make-up is visible and all is blended well.",
    "Remove extra foundation around the hairline and eyebrow using a damp cotton wool pad.",
    "If needed, apply a colour wash after the foundation.",
    "This will give a glaze over the skin.",
]


# ---------------------------------------------------------------------------
# 1. 標題框表格（SYNTHETIC 儲存格）
# ---------------------------------------------------------------------------

def _p26_rows():
    """SYNTHETIC：仿 p.26 find_tables 結果（粉底框 + 空白列 + 遮瑕框 + 結尾空白列）。合併格是 None、空格是 ''。"""
    rows = [["Application of Foundation", None]]
    rows += [[f"{i}.", s] for i, s in enumerate(FOUNDATION_STEPS, 1)]
    rows += [["", None], ["Application of Concealer", None],
             ["Concealers are used to improve or correct skin imperfections. Step 1: Apply concealer.", None], ["", None]]
    boxes = [[(0, 10 * i, 300, 10 * i + 10), None] for i in range(len(rows))]
    return rows, boxes


def test_stacked_boxes_split_into_foundation_and_concealer(extract_mod):
    rows, boxes = _p26_rows()
    pieces = extract_mod.split_stacked_boxes(rows, boxes)
    assert [p[0][0][0] for p in pieces] == ["Application of Foundation", "Application of Concealer"]
    foundation, concealer = pieces[0][0], pieces[1][0]
    assert len(foundation) == 12 and not any("Concealers are used" in (c or "") for r in foundation for c in r)
    assert len(concealer) == 2                      # 結尾空白列去掉
    assert len(pieces[0][1]) == 12                  # 座標跟著切


@pytest.mark.parametrize("rows", [
    [["a", "b"], ["", ""], ["Title", None], ["x", "y"]],                       # 第一列不是標題列
    [["Title", None], ["1.", "x"], ["", None], ["Other", None]],                # 第二段只剩 1 列
    [["Title", None], ["1.", "x"], ["2.", "y"]],                                # 沒有空白列 + 標題列
])
def test_stacked_boxes_not_split(extract_mod, rows):
    assert extract_mod.split_stacked_boxes(rows, None) == [(rows, None)]


def test_titled_step_box_has_header_and_no_review(extract_mod):
    rows, _ = _p26_rows()
    ts, reasons = extract_mod.build_table_structure(rows[:12], "pymupdf_lines")
    assert reasons == []
    assert ts.header_rows == 1 and ts.column_names == ["Application of Foundation"] * 2
    assert ts.row_labels == [f"{i}." for i in range(1, 12)] and ts.empty_cells == 0


@pytest.mark.parametrize("mutate", [
    lambda r: r.__setitem__(3, ["4.", r[3][1]]),         # 跳號（1,2,4,…）
    lambda r: r.__setitem__(5, ["5.", ""]),              # 步驟沒字
    lambda r: r.__setitem__(0, ["Application of Foundation", ""]),  # 標題列第二格是空字串（不是合併格）
    lambda r: r.__setitem__(2, ["ii.", r[2][1]]),        # 羅馬數字（Types of Blush 這類分類清單不算步驟）
])
def test_titled_step_box_rejects_broken_numbering(extract_mod, mutate):
    rows, _ = _p26_rows()
    rows = [list(r) for r in rows[:12]]
    mutate(rows)
    _, reasons = extract_mod.build_table_structure(rows, "pymupdf_lines")
    assert reasons, "結構有疑點時仍要交人工確認"


# ---------------------------------------------------------------------------
# 2. 表格旁的圖說（SYNTHETIC 行）
# ---------------------------------------------------------------------------

def _span(text, x0, x1, y0=271.4, font="NotoSerif"):
    return {"text": text, "bbox": (x0, y0, x1, y0 + 13), "size": 9.0, "font": font, "flags": 0}


def _merged_line(extract_mod, right_text):
    spans = [_span("Match the foundation with the client’s skin tone using a damp", 90.9, 375.0), _span(right_text, 412.1, 528.0)]
    return extract_mod._build_line(extract_mod._join_spans(spans), pymupdf.Rect(90.9, 271.4, 528.0, 284.4),
                                   spans, None, False, False)


def test_caption_rescued_from_table_line(extract_mod):
    table = pymupdf.Rect(61.3, 230.1, 384.5, 526.6)
    out = extract_mod.rescue_captions_from_table_lines([_merged_line(extract_mod, "Figure 1.14: Application of ")], [table])
    assert [ln.text.strip() for ln in out] == ["Match the foundation with the client’s skin tone using a damp",
                                               "Figure 1.14: Application of"]
    assert not table.intersects(out[1].bbox)


def test_non_caption_text_beside_table_not_split(extract_mod):
    """表格外不是圖說（可能真的是被截斷的儲存格）→ 不拆，交給既有的殘片檢查標 partial。"""
    table = pymupdf.Rect(61.3, 230.1, 384.5, 526.6)
    ln = _merged_line(extract_mod, "12.5 30.1")
    assert extract_mod.rescue_captions_from_table_lines([ln], [table]) == [ln]


# ---------------------------------------------------------------------------
# 3. 實作活動 vs 測驗（SYNTHETIC 文件）
# ---------------------------------------------------------------------------

def _doc(specs) -> Document:
    """specs: [(block_type, text) 或 (block_type, text, {欄位})]。"""
    m = SourceManifest(doc_id="SYN", source_path="x", source_type="pdf", sha256="0" * 64, file_size_bytes=0, extractor="test")
    blocks = []
    for i, spec in enumerate(specs):
        bt, txt = spec[0], spec[1]
        kw = spec[2] if len(spec) > 2 else {}
        extra = {"table_data": kw.pop("table_data"), "cleaned_table_data": kw.pop("cleaned_table_data")} if "table_data" in kw else {}
        blocks.append(TextBlock(doc_id="SYN", block_id=f"SYN_b{i:03d}", block_type=bt, order=i, page_number=1 + i // 10,
                                section="Introduction", raw_text=txt, cleaned_text=txt, search_text=txt,
                                heading_level=1 if bt == "heading" else None, **extra, **kw))
    return Document(stage="cleaned", manifest=m, blocks=blocks)


def _table_block():
    rows = [["Application of Foundation", None]] + [[f"{i}.", s] for i, s in enumerate(FOUNDATION_STEPS, 1)]
    text = "\n".join(" | ".join(c or "" for c in r) for r in rows)
    return ("table", text, {"table_data": rows, "cleaned_table_data": rows})


P29_STEPS = [
    "• Decant the foundation from the container onto a palette.",
    "• Match the foundation with the client’s skin tone using a damp sponge or fingers.",
    "• Cover the eyelids and lips.",
    "• Apply green cover stick or concealer before the foundation if you have any pimples or red areas.",
    "• Squeeze out some foundation onto your spatula and apply onto your hand.",
    "• If you are using your hand for applying foundation then apply a dot on the forehead, cheeks, nose and chin and jaw line and spread it evenly.",
    "• Take your foundation brush and blend the foundation downwards stroking movements. Ensure that no make-up is visible and all is blended well.",
    "• Remove extra foundation around the hairline and eyebrow by using a damp cotton wool pad.",
    "• If needed, apply a colour wash after the foundation. This will give glaze over the skin.",
]


def _practice_doc(activity_steps=P29_STEPS, with_procedure=True, with_table=True):
    specs = [("heading", "Introduction"), ("paragraph", "SYNTHETIC body text.")]
    if with_table:
        specs.append(_table_block())
    specs += [("heading", "Practical Exercises"), ("label", "Activity 1"),
              ("paragraph", "Select and apply right foundation. Materials Required: Palette, suitable foundation, concealer, spatula.")]
    if with_procedure:
        specs.append(("label", "Procedure"))
    specs += [("list_item", s) for s in activity_steps]
    specs += [("label", "Activity 2"), ("paragraph", "Discuss the uses. Materials Required: Chart."),
              ("label", "Procedure"), ("list_item", "• Write about its uses."), ("list_item", "• Discuss with peers."),
              ("list_item", "• Present to the class."),
              ("heading", "Check Your Progress"), ("label", "A. Multiple Choice Questions"),
              ("list_item", "1. Which step should be taken first when applying foundation? (a) Apply a green cover stick."),
              ("label", "B. Fill in the Blanks"), ("list_item", "1. __________ is applied to achieve a smooth complexion."),
              ("heading", "Session 2")]
    return _doc(specs)


def _by_text(doc, reasons):
    return {(b.cleaned_text or ""): reasons[b.block_id] for b in doc.blocks}


def test_practice_repeating_body_method_attached_not_exported_twice():
    doc = _practice_doc()
    reasons = classify_blocks(doc)
    r = _by_text(doc, reasons)
    assert r[_table_block()[1]] is None                                   # 正文粉底表可匯出
    for s in P29_STEPS:
        assert r[s] == "practical_activity_repeat"                        # 活動步驟不另成段
    acts = practical_activities(doc, reasons)
    assert [(a.title, a.step_match, a.matched_block_id is not None) for a in acts] == [
        ("Activity 1", (9, 9), True), ("Activity 2", (0, 3), False)]
    a1 = acts[0]
    assert a1.text.splitlines()[:3] == ["Activity 1", "Select and apply right foundation. Materials Required: Palette, "
                                        "suitable foundation, concealer, spatula.", "Procedure"]  # 標題與上下文完整
    assert "• Cover the eyelids and lips." in a1.text.splitlines()


def test_quiz_and_answers_stay_excluded():
    doc = _practice_doc()
    r = _by_text(doc, classify_blocks(doc))
    assert r["1. Which step should be taken first when applying foundation? (a) Apply a green cover stick."] == "exercise"
    assert r["1. __________ is applied to achieve a smooth complexion."] == "exercise"


def test_unmatched_classroom_activity_not_exported():
    doc = _practice_doc()
    r = _by_text(doc, classify_blocks(doc))
    assert r["• Write about its uses."] == "practical_activity_unmatched"


def test_activity_without_procedure_label_stays_exercise():
    doc = _practice_doc(with_procedure=False)
    r = _by_text(doc, classify_blocks(doc))
    assert r["• Cover the eyelids and lips."] == "exercise"


def test_activity_without_body_counterpart_is_unmatched():
    """沒有正文粉底表（例如表格仍待確認）時，活動不會自己變成可匯出。"""
    doc = _practice_doc(with_table=False)
    r = _by_text(doc, classify_blocks(doc))
    assert r["• Cover the eyelids and lips."] == "practical_activity_unmatched"


def test_reordered_steps_not_matched():
    """步驟順序倒過來就不算重複同一方法（順序是方法的一部分）。"""
    doc = _practice_doc(activity_steps=list(reversed(P29_STEPS)))
    r = _by_text(doc, classify_blocks(doc))
    assert r["• Cover the eyelids and lips."] == "practical_activity_unmatched"


def test_activity_followed_directly_by_quiz_label():
    """活動後面直接接測驗標籤（中間沒有章節標題）：活動在標籤前結束，測驗陳述句仍排除。"""
    specs = [("heading", "Introduction"), _table_block(), ("heading", "Practical Exercises"), ("label", "Activity 1"),
             ("paragraph", "Select and apply right foundation. Materials Required: Palette, suitable foundation."),
             ("label", "Procedure")] + [("list_item", s) for s in P29_STEPS] + [
             ("label", "C. State whether the following statements are true or false"),
             ("list_item", "• Foundation is applied after loose powder."),
             ("list_item", "• Cover the eyelids and lips before applying foundation."),
             ("heading", "Session 2")]
    doc = _doc(specs)
    r = _by_text(doc, classify_blocks(doc))
    assert r["• Cover the eyelids and lips."] == "practical_activity_repeat"
    assert r["• Foundation is applied after loose powder."] == "exercise"
    assert r["• Cover the eyelids and lips before applying foundation."] == "exercise"


def test_negated_steps_not_matched():
    """把步驟改成否定（Do not …）就不是同一方法，不能當重複。"""
    negated = [s.replace("• ", "• Do not ", 1) for s in P29_STEPS]
    doc = _practice_doc(activity_steps=negated)
    r = _by_text(doc, classify_blocks(doc))
    assert r["• Do not Cover the eyelids and lips."] == "practical_activity_unmatched"


def _para(text):
    return TextBlock(doc_id="SYN", block_id="SYN_t", block_type="paragraph", order=0, page_number=1,
                     raw_text=text, cleaned_text=text, search_text=text)


def test_negation_not_borrowed_from_other_step():
    """獨立複查反例（SYNTHETIC）：正文第 2 步有 not，活動把第 1 步改成 Do not cover → 不能借第 2 步的 not 算 3/3。"""
    from lipmatch.eligibility import match_steps
    body = _para("Cover the eyelids and lips. Do not touch the eyes. Blend foundation evenly.")
    n, ordered, contradicted = match_steps(
        ["Do not cover the eyelids and lips.", "Do not touch the eyes.", "Blend foundation evenly."], body)
    assert contradicted and n == 2
    assert match_steps(["Cover the eyelids and lips.", "Do not touch the eyes.", "Blend foundation evenly."],
                       body) == (3, True, False)


def test_body_negation_missing_in_step_is_contradiction():
    """反方向：正文說 Do not touch，活動說 Touch → 也是相反。"""
    from lipmatch.eligibility import match_steps
    body = _para("Cover the eyelids and lips. Do not touch the eyes. Blend foundation evenly.")
    assert match_steps(["Cover the eyelids and lips.", "Touch the eyes.", "Blend foundation evenly."], body)[2]


def test_single_opposite_step_blocks_whole_activity():
    """9 步中只有 1 步相反：8/9 雖過 80%，整個活動仍不算重複。"""
    steps = list(P29_STEPS)
    steps[2] = "• Do not cover the eyelids and lips."
    doc = _practice_doc(activity_steps=steps)
    r = _by_text(doc, classify_blocks(doc))
    assert r["• Do not cover the eyelids and lips."] == "practical_activity_unmatched"
    assert r[P29_STEPS[0]] == "practical_activity_unmatched"


def test_question_step_disqualifies_activity():
    doc = _practice_doc(activity_steps=P29_STEPS[:-1] + ["• What should you apply after the foundation?"])
    r = _by_text(doc, classify_blocks(doc))
    assert r["• Cover the eyelids and lips."] == "exercise"


def test_export_attaches_corroboration_without_changing_search_text(tmp_path):
    export_mod = _load("lipmatch_export", "05_export_rag_ready.py")
    from lipmatch.sources import SourceInfo
    src = SourceInfo(title="SYNTHETIC", authors=["SYNTHETIC"], author_citation="SYNTHETIC", year=2026,
                     verified_from="test")
    doc = _practice_doc()
    path, _ = export_mod.export(doc, src, tmp_path)
    import json
    recs = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    table = next(r for r in recs if r["block_type"] == "table")
    assert table["text"].count("Cover the eyelids and lips.") == 1          # 同一方法只灌一次
    [cu] = table["corroborating_units"]
    assert cu["title"] == "Activity 1" and cu["steps_matched"] == "9/9" and len(cu["step_block_ids"]) == 9
    assert all("corroborating_units" not in r for r in recs if r is not table)
    assert not any("Cover the eyelids and lips." in r["text"] for r in recs if r is not table)
    assert not any("Which step should be taken first" in r["text"] for r in recs)


# ---------------------------------------------------------------------------
# 4. 真實 TX001（p.26 擷取；p.26–30 清洗後分類）
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def tx001_p26_30(extract_mod, clean_mod):
    if TX001_PDF is None:
        pytest.skip("找不到 TX001 PDF（可設 LIPMATCH_RAW_DIR）")
    # 要擷取整份：只取 p.26–30 時頁邊欄「Notes」出現頁數不夠、不會被認成頁面標籤，
    # 會變成章節標題把練習區切斷（這是取頁範圍才有的假象，整份擷取時不會發生）
    return clean_mod.clean_document(extract_mod.extract_pdf(TX001_PDF, "TX001"))


def _p26(doc):
    return [b for b in doc.blocks if b.page_number == 26]


def test_tx001_p26_foundation_table_complete(tx001_p26_30):
    tables = [b for b in _p26(tx001_p26_30) if b.block_type == "table"]
    foundation = next(b for b in tables if b.cleaned_table_data[0][0] == "Application of Foundation")
    assert foundation.block_id == "TX001_p0026_b007"                     # 原定位不變
    assert foundation.parse_status == "ok" and not foundation.needs_review, foundation.review_reasons
    steps = [r[1] for r in foundation.cleaned_table_data[1:]]
    assert [r[0] for r in foundation.cleaned_table_data[1:]] == [f"{i}." for i in range(1, 12)]   # 順序
    assert steps[2] == "Cover the eyelids and lips."
    assert steps[3].endswith("if you have any pimples or red areas.")     # 條件不遺失
    assert steps[7] == "Ensure that no make-up is visible and all is blended well."   # 否定不遺失
    assert not any("concealer" in s.lower() and "Step" in s for s in steps)            # 遮瑕框不混入
    assert not any("Figure 1.14:" in (c or "") for r in foundation.cleaned_table_data for c in r)  # 圖說不混入


def test_tx001_p26_concealer_box_and_captions_separate(tx001_p26_30):
    blocks = _p26(tx001_p26_30)
    concealer = [b for b in blocks if b.block_type == "table" and b.cleaned_table_data[0][0] == "Application of Concealer"]
    assert len(concealer) == 1 and "Cover the eyelids" not in concealer[0].cleaned_text
    caps = {b.cleaned_text for b in blocks if b.block_type == "figure_caption"}
    assert caps == {"Figure 1.14: Application of cream foundation", "Figure 1.15: Application of concealer"}
    assert not any(b.cleaned_text == "cream foundation" for b in blocks)  # 不再有孤立的圖說後半


def test_tx001_p29_activity_attached_to_p26_and_quiz_excluded(tx001_p26_30):
    reasons = classify_blocks(tx001_p26_30)
    acts = practical_activities(tx001_p26_30, reasons)
    a1 = next(a for a in acts if a.pages == (28, 29) and a.title == "Activity 1")
    assert a1.matched_block_id == "TX001_p0026_b007" and a1.step_match == (9, 9)
    assert "TX001_p0029_b005" in a1.step_ids and reasons["TX001_p0029_b005"] == "practical_activity_repeat"
    assert reasons["TX001_p0026_b007"] is None
    by = {b.block_id: b for b in tx001_p26_30.blocks}
    quiz = [i for i, b in by.items() if b.page_number == 30 and (b.cleaned_text or "").startswith("1. Which step should be taken first")]
    assert quiz and all(reasons[i] == "exercise" for i in quiz)
