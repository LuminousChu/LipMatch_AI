"""multi-need-v1（2026-11-07）：一句話多個需求時拆開各查一次，保證每個需求有證據進模型。

整支測試在檢查什麼（句子是 SYNTHETIC 開發句與 40 題原句；不連資料庫、不呼叫模型）：
1. 拆句：兩個需求拆成兩句；只描述狀況的子句接到需求上；只有語氣的需求併回前一個；單一需求不拆；接續題（；）不拆。
2. 主詞：後面的需求只有泛稱（唇妝用什麼）才借第一個需求的主詞；自己有主題的不借。
3. 保證順序：各需求前 2 段依需求輪流排最前面，同一段只放一次，其餘照原本順序；原本就有的用原本的物件。
4. 設定：QA_MULTI_NEED 只接受 on／off，預設 off。
"""
from __future__ import annotations

import pytest

from app import multi_need as mn
from app import qa

TWO_NEEDS = "我在挑我面試用的唇彩顏色，我的嘴唇顏色偏深，我該怎麼辦"      # SYNTHETIC 開發句


def test_two_need_question_splits_into_two_needs():
    assert mn.split_needs(TWO_NEEDS) == ["我在挑我面試用的唇彩顏色", "我的嘴唇顏色偏深，我該怎麼辦"]
    assert mn.split_needs("我想要上班用要擦什麼顏色，而且我嘴唇偏乾，怎麼辦") == ["我想要上班用要擦什麼顏色", "我嘴唇偏乾，怎麼辦"]


@pytest.mark.parametrize("text", ["我是冷底，想擦紅色，要選哪一種紅比較適合？", "豆沙色跟奶茶色差在哪裡？",
                                  "我想要聚會用、不要太亮的唇色；改成上班", ""])
def test_single_need_or_carried_question_not_split(text):
    assert mn.split_needs(text) == []


def test_filler_only_need_merges_into_previous():
    assert mn.split_needs("很多人說法式妝就是要擦大紅唇，真的一定要嗎？如果想要法式感，唇妝和底妝要怎麼處理？") == [
        "很多人說法式妝就是要擦大紅唇，真的一定要嗎", "如果想要法式感，唇妝和底妝要怎麼處理"]


def test_subject_borrowed_only_for_generic_head():
    assert mn.split_needs("草莓女孩妝適合什麼場合？要怎麼畫，唇妝用什麼？")[1] == "草莓女孩妝 唇妝用什麼"
    assert mn.split_needs("我想要上班用要擦什麼顏色，而且我嘴唇偏乾，怎麼辦")[1] == "我嘴唇偏乾，怎麼辦"


def test_guarantee_front_round_robin_and_keeps_rest():
    base = [{"unit_id": u, "src": "full"} for u in ["A", "B", "C", "D"]]
    subs = [[{"unit_id": "X1"}, {"unit_id": "C"}, {"unit_id": "X3"}], [{"unit_id": "Y1"}, {"unit_id": "X1"}]]
    out, g = mn.guarantee_front(base, subs, lambda c: c["unit_id"])
    assert [c["unit_id"] for c in out] == ["X1", "Y1", "C", "A", "B", "D"]
    assert g == [["X1", "C"], ["Y1", "X1"]]
    assert next(c for c in out if c["unit_id"] == "C")["src"] == "full"   # 原本就有的用原本的物件


def test_config_flag(monkeypatch):
    monkeypatch.delenv("QA_MULTI_NEED", raising=False)
    assert qa.QaConfig.from_env().multi_need is False
    monkeypatch.setenv("QA_MULTI_NEED", "on")
    assert qa.QaConfig.from_env().multi_need is True and qa.QaConfig.from_env().log_view()["multi_need"] is True
    monkeypatch.setenv("QA_MULTI_NEED", "1")
    with pytest.raises(RuntimeError):
        qa.QaConfig.from_env()


def test_extra_hits_renumbered_after_first_search():
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class H:
        chunk_id: int
        rank: int
    first = [H(1, 1), H(2, 2), H(3, 8)]
    all_hits = [H(1, 1), H(2, 2), H(3, 8), H(9, 1), H(7, 2)]       # 子查詢的名次從 1 開始，會和原本重複
    out = mn.renumber_extra_hits(first, all_hits)
    assert [(h.chunk_id, h.rank) for h in out] == [(1, 1), (2, 2), (3, 8), (9, 9), (7, 10)]
    assert len({h.rank for h in out}) == len(out)


@pytest.mark.parametrize("raw,clean", [
    ("我在挑我面試用的唇彩顏色", "面試用的唇彩顏色"),          # 拿掉自述開頭
    ("我的嘴唇顏色偏深，我該怎麼辦", "嘴唇顏色偏深，怎麼辦"),
    ("我想要上班用要擦什麼顏色", "上班用要擦什麼顏色"),
    ("草莓女孩妝 唇妝用什麼", "草莓女孩妝 唇妝用什麼"),       # 沒有自述開頭：不動
    ("我想要", "我想要"),                                      # 拿完太短：用原句
])
def test_clean_query_strips_self_reference_lead(raw, clean):
    assert mn.clean_query(raw) == clean
