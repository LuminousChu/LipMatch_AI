"""fix-answers 第八批：第十四輪（匯出 15）審核後的後處理修正。

整支在測什麼（主張文字取自第五批 T4 實跑 fix-answers-40q-v3-20261108，屬實際資料，不是合成）：
  A. plain-term-v3（answer_tasks.source_term_repairs）：中文句子裡留英文 tissue → 面紙（P12）；
     來源沒有 tissue、或整句是英文時不改。
  B. product-note-v2（postprocess._result）：只刪掉附帶舉例的商品名、句子都還在時，
     系統說明改成「只拿掉商品名」，不說「有幾句…先拿掉」（LM35-I02）；整條被拿掉時照舊。
  C. focus-v6 第 11 項（answer_focus.drop_repeated_research_prefix）：同一篇文獻只在第一次保留
     「研究觀察顯示…」開頭（W09 六條 → 兩條）；第一次引用、拿掉後太短時不動。
色名參照 color-ref-v6（W03／W04）在 tests/test_color_reference.py。
不呼叫模型、不連資料庫。
"""
from __future__ import annotations

import json

from app import answer_focus
from app import answer_tasks
from app import postprocess as PP

# P12 引用的 PR015 片段（第五批送入模型的原文，節錄）
PR015_H0146 = ("Use setting powder for ultra-long wear: After applying your lipstick, lightly press a tissue over "
               "your lips and dust a translucent setting powder over the tissue.")


# ---------------------------------------------------------------------------
# A. plain-term-v3
# ---------------------------------------------------------------------------
def test_p12_english_tissue_becomes_facial_tissue():
    """P12：「先用 tissue 輕壓唇部」→「先用面紙輕壓唇部」（前後空白一併拿掉）。"""
    text, rules = answer_tasks.source_term_repairs(
        "上完唇膏後，可以先用 tissue 輕壓唇部，再用透明定妝粉輕輕拍上，這有助於固定顏色且不影響霧面質感。",
        [PR015_H0146])
    assert text == "上完唇膏後，可以先用面紙輕壓唇部，再用透明定妝粉輕輕拍上，這有助於固定顏色且不影響霧面質感。"
    assert rules == ["tissue_english_left"]
    assert answer_tasks.TERM_REPAIR_VERSION in ("plain-term-v3", "plain-term-v4", "plain-term-v5")   # fix-heldout 升 v4、fix-metrics 升 v5


def test_english_tissue_rule_negative_cases():
    """反例：來源沒 tissue 不改；整句英文（沒有中文字）不改。"""
    s = "上完唇膏後，可以先用 tissue 輕壓唇部。"
    assert answer_tasks.source_term_repairs(s, ["Blot your lips with a cotton pad."]) == (s, [])
    s2 = "Press a tissue over your lips."
    assert answer_tasks.source_term_repairs(s2, [PR015_H0146]) == (s2, [])


# ---------------------------------------------------------------------------
# B. product-note-v2
# ---------------------------------------------------------------------------
def _unit(uid, mentions=()):
    """組一個送入片段（只放後處理會讀的欄位）。"""
    return {"unit_id": uid, "text": "x", "conditions": [], "content_role": None,
            "support_scope": None, "product_mentions": list(mentions)}


def _run(claims, units):
    """把主張包成模型輸出 JSON，跑 parse_answer＋postprocess。"""
    raw = json.dumps({"insufficient": False, "claims": [{**c, "conditions": []} for c in claims],
                      "limitations": [], "clarifying_question": None}, ensure_ascii=False)
    return PP.postprocess(PP.parse_answer(raw), units, PP.Policy(kept_unit_ids=frozenset(units)))


def test_i02_name_only_edit_uses_name_only_note():
    """LM35-I02：只拿掉「（如Superstar Lips）」、句子保留 → 說明改成只拿掉商品名。"""
    units = {"PR005_h0101": _unit("PR005_h0101"), "PR021_h0316": _unit("PR021_h0316", ["Superstar Lips"])}
    out = _run([{"text": "唇蜜包含的顏色色素比唇膏少，且持久度較低。", "unit_ids": ["PR005_h0101"]},
                {"text": "唇膏與唇蜜的結合產物（如Superstar Lips）可同時擁有唇膏的奶油質地與唇蜜的閃耀效果。",
                 "unit_ids": ["PR021_h0316"]}], units)
    assert [c["text"] for c in out["claims"]][1] == "唇膏與唇蜜的結合產物可同時擁有唇膏的奶油質地與唇蜜的閃耀效果。"
    assert PP.PRODUCT_NAME_ONLY_NOTE in out["system_notes"]
    assert PP.PRODUCT_NOTE not in out["system_notes"]


def test_whole_claim_removed_keeps_original_note():
    """整條在講商品而被拿掉時，照舊說「有幾句在講具體商品…先拿掉」。"""
    units = {"seg1:346": _unit("seg1:346", ["L’Oréal Paris Infallible Matte Resistance Liquid Lipstick"]),
             "seg1:42": _unit("seg1:42")}
    out = _run([{"text": "L’Oréal Paris Infallible Matte Resistance Liquid Lipstick 具有高色素沉澱、可持續長達 16 小時。",
                 "unit_ids": ["seg1:346"]},
                {"text": "唇部產品可以增加顏色、滋養嘴唇。", "unit_ids": ["seg1:42"]}], units)
    assert PP.PRODUCT_NOTE in out["system_notes"] and PP.PRODUCT_NAME_ONLY_NOTE not in out["system_notes"]


# ---------------------------------------------------------------------------
# C. focus-v6 第 11 項
# ---------------------------------------------------------------------------
LEEDS, JP = "AR010_p0004_b002", "AR011_p0011_b003"   # 兩篇不同文獻（編號只取文件部分比對）
W09_CLAIMS = [
    ("研究觀察顯示，唇膏顏色會顯著影響臉部皮膚的感知亮度與紅潤度。", [LEEDS]),
    ("研究觀察指出，紅色唇膏通常能增加臉部的感知亮度，並減少皮膚的暗沉感。", [JP]),
    ("研究觀察發現，紅色唇膏能讓臉部看起來更紅潤，且與臉部被認為更美觀有正相關。", [JP]),
    ("研究觀察顯示，在某些情況下，與皮膚顏色較淺的唇色會讓膚色看起來更明亮。", ["AR010_p0066_b001"]),
    ("研究觀察指出，紅色、粉紅色和橙色唇膏會讓臉部看起來更紅潤。", ["AR011_p0005_b001"]),
    ("研究觀察發現，紫色唇膏可能因為含有藍色成分，會對臉部的美觀印象產生負面影響。", [JP]),
]


def test_w09_research_prefix_kept_once_per_document():
    """W09：六條都以「研究觀察…」開頭 → 兩篇文獻各在第一次保留，其餘四條拿掉引導語。"""
    claims = [{"claim_no": i, "text": t, "unit_ids": u} for i, (t, u) in enumerate(W09_CLAIMS, 1)]
    out, flag = answer_focus.drop_repeated_research_prefix(claims)
    texts = [c["text"] for c in out]
    assert texts[0].startswith("研究觀察顯示") and texts[1].startswith("研究觀察指出")
    assert texts[2] == "紅色唇膏能讓臉部看起來更紅潤，且與臉部被認為更美觀有正相關。"
    assert texts[3] == "在某些情況下，與皮膚顏色較淺的唇色會讓膚色看起來更明亮。"
    assert sum(t.startswith("研究") for t in texts) == 2
    assert flag["flag"] == "research_prefix_repeated" and [c["claim_no"] for c in flag["claims"]] == [3, 4, 5, 6]


def test_research_prefix_negative_cases():
    """反例：各引用不同文獻不動；新文獻和舊文獻一起引用時不動；拿掉後太短不動。"""
    claims = [{"claim_no": 1, "text": "研究指出，紅唇提亮膚色。", "unit_ids": ["AR001_p1"]},
              {"claim_no": 2, "text": "研究指出，粉唇顯得柔和。", "unit_ids": ["AR002_p1"]},
              {"claim_no": 3, "text": "研究發現，橘色也讓臉看起來紅潤。", "unit_ids": ["AR001_p2", "AR003_p1"]},
              {"claim_no": 4, "text": "研究發現，偏冷。", "unit_ids": ["AR001_p3"]}]
    out, flag = answer_focus.drop_repeated_research_prefix(claims)
    assert out == claims and flag is None


def test_apply_runs_research_prefix_step():
    """answer_focus.apply 會跑第 11 項，版本 focus-v6。"""
    delivered = {"claims": [{"claim_no": i, "text": t, "unit_ids": u, "role": "direct"}
                            for i, (t, u) in enumerate(W09_CLAIMS, 1)],
                 "limitations": [], "flags": [], "removed": [], "status": "delivered"}
    out = answer_focus.apply(delivered, "擦不同顏色的口紅，會讓臉看起來比較白或比較紅潤嗎？")
    assert out["focus_version"] == answer_focus.FOCUS_VERSION   # fix-heldout 升 focus-v7
    assert sum(c["text"].startswith("研究") for c in out["claims"]) == 2
