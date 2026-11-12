"""修復 B：候選取得、重排、生成預算裝箱三段分離（D16；契約 contract.retrieval_policy）。

舊流程（研究 gen_experiment.retrieve，原流程 A）在向量掃描時就套 2,048 token 生成預算：
依名次累加，遇到第一個放不下的片段就 break，後面的候選全部看不到。前方一個長段
（seg1:138 1,639 token、seg1:1297 571 token）就能讓候選池只剩 2～5 段（W01、H06）。

本模組把三件事拆開：
  1. retrieve_candidates：只依向量名次取前 k_pool 個完整單位，不看長度、不受生成預算影響；
  2. rerank：重排分數由注入的 reranker 計算，保留原向量名次（pool_rank），同分依 pool_rank；
  3. pack_budget：最後才裝箱，只放完整單位、不截斷；放不下就略過（記原因）並繼續看下一個；
     不為湊滿 max_units 而加入呼叫端沒選的單位。
另有 legacy_stop_pool 原樣重現舊規則，只供修前失敗測試與對照，不得用在新流程。

k_pool 的決定：不輪流試 k。reachability() 依保存名次＋一次新表示的實際名次，報每個目標單位
在凍結 k 下能否入池；池外就如實記「pool_outside」（未解），不靠後段選段器補救。

本模組只用標準函式庫，不連資料庫、不呼叫模型：embedder／store／reranker／token 計數都由呼叫端注入
（後端接線由 D 負責；實驗 runner 在 scripts/repair_b/）。
"""
from __future__ import annotations

import dataclasses
import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

POLICY_VERSION = "retrieval-policy-v1"
DEFAULT_MAX_TOKENS = 2048   # 生成預算（cl100k），沿用原流程 A 的上下文上限
DEFAULT_MAX_UNITS = 7       # 上限，不是目標數量


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Candidate:
    """一個候選支持單位。pool_rank 是向量名次（1 起算），重排後不改；rerank_rank 是重排名次。"""

    unit_id: str
    text: str
    pool_rank: int
    tokens: int
    vector_score: float | None = None
    rerank_score: float | None = None
    rerank_rank: int | None = None
    meta: Mapping[str, Any] = field(default_factory=dict)  # source_id、定位、conditions、content_role 等契約欄位
    tokens_source: str = "given"  # cl100k（A 匯出 tokens_cl100k）｜counter（注入計數器）｜approx（後備估計）｜given


class Embedder(Protocol):
    model: str
    dim: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class CandidateStore(Protocol):
    """contract-v1：D 的 retrieval.CandidateStore(conn, role)。

    search(query, k, embedder) 只在角色可用、active 索引中內容相符的片段裡做向量搜尋（治理條件都在 store 內），
    回傳依距離排序的 Hit（需有 chunk_id、chunk_key、score）；units(hits) 回傳這些片段的 DB 列
    （chunk_key、quote_text、doc_key、version_label、support_unit…）。本模組不放寬任何治理條件。
    """

    def search(self, query: str, k: int, embedder: Embedder) -> list[Any]: ...

    def units(self, hits: Iterable[Any]) -> list[Mapping[str, Any]]: ...


class Reranker(Protocol):
    model: str

    def score(self, query: str, texts: Sequence[str]) -> list[float]: ...


TokenCounter = Callable[[str], int]

_CJK = re.compile(r"[　-鿿豈-﫿＀-￯]")
_WORD = re.compile(r"[A-Za-z0-9]+|[^\sA-Za-z0-9　-鿿豈-﫿＀-￯]")


def approx_tokens(text: str) -> int:
    """沒有 tokenizer 時的保守估計：中日韓字元各算 1，英數字詞每 4 字元算 1（至少 1），標點各算 1。

    只作後備；實驗與驗收要用與 2,048 基準相同的 cl100k 計數（runner 注入）。
    """
    n = len(_CJK.findall(text))
    for w in _WORD.findall(_CJK.sub(" ", text)):
        n += max(1, math.ceil(len(w) / 4)) if w[0].isalnum() else 1
    return n


# ---------------------------------------------------------------------------
# 1. 候選取得
# ---------------------------------------------------------------------------
def retrieve_candidates(query: str, *, k_pool: int, embedder: Embedder, store: CandidateStore,
                        count_tokens: TokenCounter | None = None) -> list[Candidate]:
    """取向量前 k_pool 名的完整單位。不套生成預算：長段照收，不會讓後面的候選停止。

    Candidate.unit_id＝DB chunk_key、text＝quote_text（原文不改寫；D 會再比對，不符判 pipeline_contract_violation）。
    tokens 用 support_unit.tokens_cl100k；沒有才用 approx_tokens 並標 tokens_source=approx。
    """
    if not query.strip():
        raise PolicyError("查詢不可為空")
    if k_pool < 1:
        raise PolicyError("k_pool 必須 ≥ 1")
    hits = list(store.search(query, k_pool, embedder))
    rows = {r["chunk_id"]: r for r in store.units(hits)}
    out_rows = []
    for h in hits:
        r = rows.get(h.chunk_id)
        if r is None:
            raise PolicyError(f"store 沒有交回 {h.chunk_key} 的原文")
        su = r.get("support_unit") or {}
        # 2026-10-26 實機發現：D 的 CandidateStore.units() 交回的是 unit_view 攤平列（沒有 support_unit 子物件），
        # 原本 meta 的條件／內容角色在應用裡一律是空的。片段屬性改成子物件沒有就讀攤平列。
        # 2026-10-26 之後：tokens 也從攤平列讀 cl100k（原本應用裡一直是估計值，比 cl100k 多約 30～40%，裝箱過早用完預算）
        attrs = su or r
        out_rows.append({"unit_id": r["chunk_key"], "text": r["quote_text"], "score": h.score,
                         "tokens": attrs.get("tokens_cl100k"),
                         "tokens_source": "cl100k" if attrs.get("tokens_cl100k") is not None else None,
                         "meta": {"chunk_id": r["chunk_id"], "source_id": r.get("doc_key"),
                                  "version_label": r.get("version_label"),
                                  "conditions": list(attrs.get("conditions") or []),
                                  "content_role": attrs.get("content_role"),
                                  # need-rel-v1（2026-10-26）：裝箱前的需求排序要讀片段屬性標籤
                                  "support_scope": attrs.get("support_scope"),
                                  "product_mentions": list(attrs.get("product_mentions") or [])}})
    return candidates_from_rows(out_rows, k_pool=k_pool, count_tokens=count_tokens)
# 核心邏輯：查詢只由 store 編碼一次；store 負責治理過濾與排序；本函式不看 token 預算，所以不可能「遇長段停止」。


def candidates_from_rows(rows: Iterable[Mapping[str, Any]], *, k_pool: int,
                         count_tokens: TokenCounter | None = None) -> list[Candidate]:
    """把已排序的檢索列轉成 Candidate（pool_rank 依出現順序）；重複 unit_id 只留第一次。"""
    count = count_tokens or approx_tokens
    out: list[Candidate] = []
    seen: set[str] = set()
    for row in rows:
        uid = str(row["unit_id"])
        if uid in seen:
            continue
        seen.add(uid)
        text = row["text"]
        if not isinstance(text, str) or not text.strip():
            raise PolicyError(f"{uid} 沒有原文")
        tokens = row.get("tokens")
        out.append(Candidate(unit_id=uid, text=text, pool_rank=len(out) + 1,
                             tokens=int(tokens) if tokens is not None else count(text),
                             vector_score=row.get("score"), meta=dict(row.get("meta") or {}),
                             tokens_source=row.get("tokens_source") or (
                                 "given" if tokens is not None else "counter" if count_tokens else "approx")))
        if len(out) >= k_pool:
            break
    return out


# ---------------------------------------------------------------------------
# 2. 重排
# ---------------------------------------------------------------------------
def rerank(query: str, candidates: Sequence[Candidate], *, reranker: Reranker) -> list[Candidate]:
    """依重排分數由高到低排序；同分依 pool_rank（穩定）。每個候選保留 pool_rank 並寫入 rerank_score／rank。"""
    if not candidates:
        return []
    scores = reranker.score(query, [c.text for c in candidates])
    if len(scores) != len(candidates):
        raise PolicyError("reranker 回傳分數數量與候選數不符")
    return apply_rerank_scores(candidates, scores)


def apply_rerank_scores(candidates: Sequence[Candidate], scores: Sequence[float]) -> list[Candidate]:
    """用已算好的分數（例如保存分數）排序；與 rerank() 同一排序規則，供重用保存分數時不重算。"""
    if len(scores) != len(candidates):
        raise PolicyError("分數數量與候選數不符")
    scored = [dataclasses.replace(c, rerank_score=float(s)) for c, s in zip(candidates, scores)]
    scored.sort(key=lambda c: (-c.rerank_score, c.pool_rank))
    return [dataclasses.replace(c, rerank_rank=i) for i, c in enumerate(scored, 1)]


# ---------------------------------------------------------------------------
# 3. 生成預算裝箱
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PackPlan:
    packed: list[Candidate]
    skipped: list[dict]          # [{unit_id, reason, tokens, remaining}]
    used_tokens: int
    max_tokens: int
    max_units: int


def plan_pack(selected: Sequence[Candidate], *, max_tokens: int = DEFAULT_MAX_TOKENS,
              max_units: int = DEFAULT_MAX_UNITS) -> PackPlan:
    """依呼叫端給的順序裝箱，回傳裝入與略過（含原因）。"""
    if max_tokens < 1 or max_units < 1:
        raise PolicyError("max_tokens、max_units 必須 ≥ 1")
    packed, skipped, used = [], [], 0
    for c in selected:
        if len(packed) >= max_units:
            skipped.append({"unit_id": c.unit_id, "reason": "unit_cap", "tokens": c.tokens,
                            "remaining": max_tokens - used})
            continue
        if c.tokens > max_tokens:
            skipped.append({"unit_id": c.unit_id, "reason": "unit_exceeds_budget", "tokens": c.tokens,
                            "remaining": max_tokens - used})
            continue
        if used + c.tokens > max_tokens:
            skipped.append({"unit_id": c.unit_id, "reason": "exceeds_remaining_budget", "tokens": c.tokens,
                            "remaining": max_tokens - used})
            continue
        packed.append(c)
        used += c.tokens
    return PackPlan(packed=packed, skipped=skipped, used_tokens=used, max_tokens=max_tokens, max_units=max_units)
# 核心邏輯：
# 1. 只放完整單位，從不截斷原文（截斷可能切掉條件句）。
# 2. 放不下就略過、記原因、繼續看下一個（不是 break）；達到 max_units 後其餘記 unit_cap。
# 3. 只裝呼叫端傳入的單位：選段器移除的不會被補回來湊滿 7 段。


def budget_window(candidates: Sequence[Candidate], max_tokens: int) -> tuple[list[Candidate], list[dict]]:
    """依給定順序放完整單位到 max_tokens，放不下略過繼續（不限段數）。選段輸入視窗與裝箱共用同一規則。"""
    plan = plan_pack(candidates, max_tokens=max_tokens, max_units=max(1, len(candidates)))
    return plan.packed, plan.skipped


def pack_budget(selected: Sequence[Candidate], *, max_tokens: int = DEFAULT_MAX_TOKENS,
                max_units: int = DEFAULT_MAX_UNITS) -> list[Candidate]:
    """契約介面：回傳裝入的單位（略過原因請用 plan_pack 取得）。"""
    return plan_pack(selected, max_tokens=max_tokens, max_units=max_units).packed


# ---------------------------------------------------------------------------
# 舊規則重現（只供修前失敗測試與對照）
# ---------------------------------------------------------------------------
def legacy_stop_pool(ranked: Sequence[Candidate], budget: int = DEFAULT_MAX_TOKENS) -> tuple[list[Candidate], Candidate | None]:
    """原樣重現 gen_experiment.retrieve：依名次累加，遇到第一個放不下的就停止。回傳（候選池, 停止片段）。"""
    pool, used = [], 0
    for c in ranked:
        if used + c.tokens > budget:
            return pool, c
        pool.append(c)
        used += c.tokens
    return pool, None


# ---------------------------------------------------------------------------
# 短名單可達性（k_pool 凍結依據）
# ---------------------------------------------------------------------------
def reachability(target_ranks: Mapping[str, int | None], k_pool: int) -> dict[str, dict]:
    """每個目標單位在固定 k_pool 下能否入池。rank 為 None 代表新表示中沒有這個單位（記 missing）。

    池外記 pool_outside：代表這個修法對該目標未解，不能交給選段器或回答層宣稱已修。
    """
    out = {}
    for uid, rank in target_ranks.items():
        if rank is None:
            out[uid] = {"rank": None, "in_pool": False, "status": "missing_in_representation"}
        elif rank <= k_pool:
            out[uid] = {"rank": rank, "in_pool": True, "status": "reachable"}
        else:
            out[uid] = {"rank": rank, "in_pool": False, "status": "pool_outside"}
    return out


def freeze_k_pool(target_ranks: Mapping[str, int | None], *, k_min: int, k_cap: int) -> dict:
    """依一次新表示的實際名次決定唯一 k_pool（規則事先固定，不試多個 k）：

    k_pool = clamp(落在 k_cap 內的目標最大名次, k_min, k_cap)。名次超過 k_cap 的目標直接記未解，
    不為了它把 k 拉大。回傳 k_pool 與每個目標的可達性。
    """
    if not 1 <= k_min <= k_cap:
        raise PolicyError("需要 1 ≤ k_min ≤ k_cap")
    within = [r for r in target_ranks.values() if r is not None and r <= k_cap]
    k = min(k_cap, max([k_min, *within]))
    return {"policy_version": POLICY_VERSION, "k_pool": k, "k_min": k_min, "k_cap": k_cap,
            "targets": reachability(target_ranks, k)}


def selection_reachability(ranked: Sequence[Candidate], targets: Iterable[str], *, k_pool: int,
                           max_input_tokens: int) -> dict[str, dict]:
    """應用路徑不重排（contract-v1）：選段器只看「向量名次順序、在 max_input_tokens 內放得下」的完整單位。

    每個目標回 status：judged（會送給選段器）｜outside_selection_window（進池但放不進選段輸入）｜
    pool_outside｜missing_in_representation。只有 judged 的目標才有機會被保留。
    """
    pool = list(ranked[:k_pool])
    window, _ = budget_window(pool, max_input_tokens)
    rank = {c.unit_id: c.pool_rank for c in ranked}
    in_pool, judged = {c.unit_id for c in pool}, {c.unit_id for c in window}
    out = {}
    for uid in targets:
        r = rank.get(uid)
        status = ("missing_in_representation" if r is None else "judged" if uid in judged
                  else "outside_selection_window" if uid in in_pool else "pool_outside")
        out[uid] = {"rank": r, "status": status}
    return out

