"""citation-repair-v1（2026-11-07）：主張漏標的送入證據用嵌入相似度補上引用（只加不刪）。

用假的嵌入函式（依關鍵字給固定向量），只測規則本身：門檻、只加不刪、每條最多補 1 段、失敗時不動、開關解析。
實際門檻效果見 data/review/ragas-exp2-20261107/citation_repair_probe.py 與 replay_citation_repair.py（Git 外）。
"""
from __future__ import annotations

import math

import pytest

from app import citation_repair as CR
from app import qa

# 三個主題各一個方向的向量；文字含哪個關鍵字就偏向哪個方向
_AXES = {"喜氣": (1.0, 0.0, 0.0), "假日": (0.0, 1.0, 0.0), "辦公室": (0.0, 0.0, 1.0)}


def fake_embed(texts):
    """假的嵌入：每個關鍵字出現一次就在對應軸加 1，另加一點共同成分（避免零向量）。"""
    out = []
    for t in texts:
        v = [0.1, 0.1, 0.1]
        for key, axis in _AXES.items():
            if key in t:
                v = [a + b for a, b in zip(v, axis)]
        out.append(v)
    return out


EVIDENCE = [("u1", "過年包紅包很喜氣"), ("u2", "紅色象徵喜氣與好運"), ("u3", "假日適合濃豔紅唇"), ("u4", "辦公室適合豆沙色")]


def _delivered(claims):
    """最小的 Delivered：claims 是（主張文字, 引用 unit_ids）清單。"""
    return {"status": "delivered", "flags": [],
            "claims": [{"claim_no": i, "text": t, "unit_ids": list(ids), "role": "answer"}
                       for i, (t, ids) in enumerate(claims, 1)]}


def test_adds_missing_citation_when_gap_exceeds_delta():
    """主張講辦公室、卻只引用講假日的 u3：u4 相似度明顯較高 → 補上 u4，原本的 u3 保留。"""
    out, log = CR.repair(_delivered([("辦公室擦豆沙色最得體", ["u3"])]), EVIDENCE,
                         embed_queries=fake_embed, embed_docs=fake_embed)
    assert out["claims"][0]["unit_ids"] == ["u3", "u4"]
    assert log["added"] and log["added"][0]["unit_id"] == "u4" and log["checked"] == 1
    assert out["flags"][-1]["flag"] == "citation_added"
    assert out["citation_repair_version"] == CR.CITATION_REPAIR_VERSION


def test_no_change_when_cited_is_already_best():
    """已引用的就是最相似的段落 → 不補。"""
    d = _delivered([("假日可以擦濃豔紅唇", ["u3"])])
    out, log = CR.repair(d, EVIDENCE, embed_queries=fake_embed, embed_docs=fake_embed)
    assert out["claims"][0]["unit_ids"] == ["u3"] and log["added"] == []


def test_gap_below_delta_not_added():
    """相似度差沒超過門檻 → 不補（門檻調到 0.99 時，上面的例子也不補）。"""
    out, log = CR.repair(_delivered([("辦公室擦豆沙色最得體", ["u3"])]), EVIDENCE,
                         embed_queries=fake_embed, embed_docs=fake_embed, delta=0.99)
    assert out["claims"][0]["unit_ids"] == ["u3"] and log["added"] == []


def test_adds_at_most_one_and_never_removes():
    """同時差很多的段落有兩段（u1、u2 都講喜氣）→ 只補最相似的一段；原引用不刪。"""
    out, _ = CR.repair(_delivered([("紅色象徵喜氣", ["u4"])]), EVIDENCE, embed_queries=fake_embed, embed_docs=fake_embed)
    ids = out["claims"][0]["unit_ids"]
    assert ids[0] == "u4" and len(ids) == 2 and ids[1] in {"u1", "u2"}


def test_embedding_failure_leaves_answer_unchanged():
    """嵌入服務失敗 → 不補標、記錯誤類型，交付內容不變。"""
    def boom(texts):
        """模擬嵌入服務斷線。"""
        raise ConnectionError("down")
    d = _delivered([("辦公室擦豆沙色最得體", ["u3"])])
    out, log = CR.repair(d, EVIDENCE, embed_queries=boom, embed_docs=boom)
    assert out["claims"] == d["claims"] and log["error"] == "ConnectionError"


def test_original_not_mutated_and_single_evidence_skipped():
    """不改傳進來的物件；只有一段證據時沒有別段可補，直接跳過。"""
    d = _delivered([("辦公室擦豆沙色最得體", ["u3"])])
    CR.repair(d, EVIDENCE, embed_queries=fake_embed, embed_docs=fake_embed)
    assert d["claims"][0]["unit_ids"] == ["u3"]
    out, log = CR.repair(d, EVIDENCE[2:3], embed_queries=fake_embed, embed_docs=fake_embed)
    assert out["claims"][0]["unit_ids"] == ["u3"] and log["checked"] == 0


def test_cosine_zero_vector():
    """零向量相似度記 0，不除以零。"""
    assert CR.cosine([0, 0], [1, 0]) == 0.0
    assert math.isclose(CR.cosine([1, 0], [1, 0]), 1.0)


def test_config_switch(monkeypatch):
    """QA_CITATION_REPAIR：預設 off；on 打開；其他值直接報錯（不悄悄用預設）。"""
    monkeypatch.delenv("QA_CITATION_REPAIR", raising=False)
    assert qa.QaConfig.from_env().citation_repair is False
    monkeypatch.setenv("QA_CITATION_REPAIR", "on")
    cfg = qa.QaConfig.from_env()
    assert cfg.citation_repair is True and cfg.log_view()["citation_repair"] is True
    monkeypatch.setenv("QA_CITATION_REPAIR", "yes")
    with pytest.raises(RuntimeError):
        qa.QaConfig.from_env()
