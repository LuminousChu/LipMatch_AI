"""引用補標（citation-repair-v1，2026-11-07）。

要解決的問題：
  模型有時把另一段送入證據的內容寫進主張，卻只標了別段（漏標），或標錯段（exp2 40 題判讀：N05 c1 象徵意涵在 E2、只標 E1；
  P08 c1 內容在 E5、標成 E9）。ALCE 的引用完整性（Citation Recall）看「引用的證據合起來是否完整支持這句」，
  漏標的那段補上，這句就完整了；使用者在出處也看得到真正的來源。

做法（只加不刪、不改主張文字、不呼叫生成模型）：
  1. 主張用查詢端嵌入、本次送入模型的每段證據原文用文件端嵌入（和檢索同一個 xVITA 模型與前綴）。
  2. 每條主張：cited＝已引用證據的最高相似度；best＝沒引用的送入證據中最相似的那段。
  3. best − cited > delta（預設 0.15）才把 best 補進這條主張的引用；每條最多補 1 段。
  4. 原本的引用一律保留（標錯的那段也不刪：判斷「錯」需要讀懂內容，這裡不做）。
  5. 嵌入失敗就整步跳過、記錯誤，回答照原樣交付（不讓請求失敗）。

門檻依據：data/review/ragas-exp2-20261107/citation_repair_probe.py——exp2 143 條有引用的主張，
delta=0.15 只補到 N05 c1（0.333→E2 0.539）、P08 c1（0.466→E5 0.790），其他 141 條不動；
這是看 40 題調參題定的，只有 2 個正例，要用獨立驗收題確認。
"""
from __future__ import annotations

import math
import time
from typing import Any, Callable, Mapping, Sequence

CITATION_REPAIR_VERSION = "citation-repair-v1"
DEFAULT_DELTA = 0.15

EmbedFn = Callable[[Sequence[str]], list[list[float]]]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """兩個向量的餘弦相似度；任一為零向量時回 0。"""
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def best_addition(claim_vec: Sequence[float], cited: Sequence[str], ev_vecs: Mapping[str, Sequence[float]],
                  delta: float) -> tuple[str, float, float] | None:
    """一條主張要不要補標：回傳（要補的 unit_id、它的相似度、已引用的最高相似度），不用補回 None。

    已引用的段落不在本次送入證據裡（理論上不會發生）就不判斷，回 None。"""
    cited_sims = [cosine(claim_vec, ev_vecs[u]) for u in cited if u in ev_vecs]
    others = {u: cosine(claim_vec, v) for u, v in ev_vecs.items() if u not in cited}
    if not cited_sims or not others:
        return None
    best = max(others, key=others.get)
    if others[best] - max(cited_sims) > delta:
        return best, others[best], max(cited_sims)
    return None


def repair(delivered: Mapping[str, Any], evidence: Sequence[tuple[str, str]], *, embed_queries: EmbedFn,
           embed_docs: EmbedFn, delta: float = DEFAULT_DELTA) -> tuple[dict, dict]:
    """回傳（補標後的 Delivered 副本, 日誌）。evidence 是本次送入模型的（unit_id, 原文），依送入順序。

    日誌：version、delta、checked（判斷了幾條）、added（補了哪些：claim_no、unit_id、兩個相似度）、latency_ms、error。"""
    t0 = time.perf_counter()
    out = dict(delivered)
    log: dict[str, Any] = {"version": CITATION_REPAIR_VERSION, "delta": delta, "checked": 0, "added": [],
                           "latency_ms": None, "error": None}
    claims = [dict(c) for c in out.get("claims") or []]
    targets = [c for c in claims if c.get("unit_ids")]
    if not targets or len(evidence) < 2:
        log["latency_ms"] = int((time.perf_counter() - t0) * 1000)
        return out, log
    try:
        ev_list = embed_docs([text for _, text in evidence])
        claim_list = embed_queries([c.get("text") or "" for c in targets])
    except Exception as exc:   # 嵌入服務失敗：不補標，照原樣交付
        log["error"] = type(exc).__name__
        log["latency_ms"] = int((time.perf_counter() - t0) * 1000)
        return out, log
    ev_vecs = {u: v for (u, _), v in zip(evidence, ev_list)}
    flags = list(out.get("flags") or [])
    for c, vec in zip(targets, claim_list):
        log["checked"] += 1
        hit = best_addition(vec, c["unit_ids"], ev_vecs, delta)
        if hit is None:
            continue
        unit, sim_add, sim_cited = hit
        c["unit_ids"] = list(c["unit_ids"]) + [unit]
        item = {"claim_no": c.get("claim_no"), "unit_id": unit, "sim_added": round(sim_add, 4),
                "sim_cited_max": round(sim_cited, 4)}
        log["added"].append(item)
        flags.append({"flag": "citation_added", **item})
    out.update(claims=claims, flags=flags, citation_repair_version=CITATION_REPAIR_VERSION)
    log["latency_ms"] = int((time.perf_counter() - t0) * 1000)
    return out, log
# 核心邏輯：只「加」引用、不刪不改；補上的一定是本次送入模型的段落（qa 之後的引用範圍檢查照常通過）。
# 相似度差要超過 delta 才補，避免把「同主題但沒講這件事」的段落也掛上去（降低引用準確性）。
