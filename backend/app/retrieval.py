"""S4 檢索：索引版本、可用性查詢、向量搜尋、送模型前再驗證、召回紀錄。

原則：
- 只在「可檢索片段」（searchable_chunks：已核准、啟用、版本已發布、文件啟用且在有效期間、
  在 active 索引中且內容指紋相符）裡搜尋，再依登入角色限制 visibility。先過濾再排序，
  不是先取前 k 名再過濾（避免受限片段佔名額，也避免它們進入任何中間結果）。
- 查詢向量必須來自與索引相同的模型與維度，否則拒絕（不相容向量不混用）。
- 送進模型前用 revalidate() 重查一次；搜尋與送出之間被下架或改權限的片段會被移除。
- 嵌入模型由環境變數設定（model_adapters：xVITA／llama.cpp）；未設定或設定不完整時 get_embedder()
  回報 EmbeddingUnavailable（列出缺漏），不以假向量代替。
- 768 維索引（0006）另存 chunk_embeddings_v768／searchable_chunks_v768，依 active 索引的維度選表；
  768 維索引另比對嵌入指紋（模型檔 SHA-256、文件／查詢前綴），不同就不相容、不混用。
- hybrid-v1（2026-10-27，參照 RTU-M4）：可選的兩路檢索。向量路照送中文原句；關鍵詞路拿查詢端術語表展開的英文說法
  對 search_text 做 PostgreSQL 英文全文比對（即時 to_tsvector，不建索引、不需遷移）；兩路以 RRF 合併。
  關鍵詞路和向量路查同一個可檢索 view、同一套角色與 active 索引條件；合併後每段的 distance 仍是真的 cosine 距離。
- hybrid-v2（2026-10-27）：泛稱詞（lipstick 這類品類詞）降權——只命中泛稱詞的片段不算關鍵詞命中；哪些算泛稱由呼叫端決定。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Protocol, Sequence

import psycopg
from psycopg.types.json import Jsonb

from . import model_adapters

# 角色可看到的文件 visibility；角色只來自後端認證（users.role），不讀請求欄位
ROLE_VISIBILITY: dict[str, tuple[str, ...]] = {"user": ("all",), "admin": ("all", "admin")}


class RetrievalError(RuntimeError):
    pass


class EmbeddingUnavailable(RetrievalError):
    pass


class IncompatibleIndex(RetrievalError):
    pass


class IndexNotReady(RetrievalError):
    pass


class Embedder(Protocol):
    model: str
    dim: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


# 維度 → (向量表, 可檢索 view)。1024 維沿用 0001／0004 的結構；768 維是 0006 新增的分表。
_STORES: dict[int, tuple[str, str]] = {
    1024: ("chunk_embeddings", "searchable_chunks"),
    768: ("chunk_embeddings_v768", "searchable_chunks_v768"),
}


# 可建向量的可用片段：排除「只供引用」的母段（A 以 #r 子範圍拆開多情境段落時，母段保留供引用、不進檢索）
_INDEXABLE = "NOT coalesce((attributes ->> 'citation_only')::boolean, false)"


def _store(dim: int) -> tuple[str, str]:
    if dim not in _STORES:
        raise IncompatibleIndex(f"沒有 {dim} 維的向量表（目前只有 {sorted(_STORES)}）")
    return _STORES[dim]


def _adapter(env=None) -> model_adapters.EmbeddingAdapter:
    try:
        return model_adapters.embedding_adapter_from_env(env)
    except model_adapters.AdapterNotConfigured as exc:
        raise EmbeddingUnavailable(f"嵌入模型未設定（DEC-11）：{exc}") from None


def get_embedder(env=None) -> Embedder:
    """查詢端嵌入模型（加查詢前綴）。未設定或設定不完整 → EmbeddingUnavailable，不以假向量代替。"""
    return _adapter(env).queries


def get_document_embedder(env=None) -> Embedder:
    """文件端嵌入模型（加文件前綴），建索引用。"""
    return _adapter(env).documents


def _vector_literal(vec: Sequence[float], dim: int) -> str:
    if len(vec) != dim:
        raise IncompatibleIndex(f"向量維度 {len(vec)} 與索引維度 {dim} 不符")
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def _visibility(role: str) -> list[str]:
    if role not in ROLE_VISIBILITY:
        raise RetrievalError(f"未知角色：{role}")
    return list(ROLE_VISIBILITY[role])


# ---------------------------------------------------------------------------
# 索引版本
# ---------------------------------------------------------------------------
def active_index(conn: psycopg.Connection) -> dict | None:
    return conn.execute(
        """SELECT id, label, embedding_model, embedding_dim, embedding_fingerprint
             FROM index_versions WHERE status = 'active'"""
    ).fetchone()


def check_compatible(index: dict, embedder: Embedder) -> None:
    if index["embedding_model"] != embedder.model or index["embedding_dim"] != embedder.dim:
        raise IncompatibleIndex(
            f"查詢模型 {embedder.model}/{embedder.dim} 與索引 {index['label']} "
            f"的 {index['embedding_model']}/{index['embedding_dim']} 不相容，不能混用"
        )
    expected = index.get("embedding_fingerprint")
    if expected is not None and getattr(embedder, "fingerprint", None) != expected:
        raise IncompatibleIndex(
            f"查詢模型的嵌入指紋（模型檔 SHA-256／前綴）與索引 {index['label']} 不同，不能混用")
# 核心邏輯：名稱與維度相同還不夠；索引有指紋（0006 起 768 維必有）時，模型檔與前綴也必須一模一樣。


def build_index(conn: psycopg.Connection, label: str, embedder: Embedder, batch: int = 32,
                notes: str | None = None) -> dict:
    """為目前所有可用片段建立新的 building 索引（不影響 active 索引）。連線須使用 dict_row。

    768 維：embedder 必須是文件端（side=document）且帶嵌入指紋；每筆向量一併寫入實際嵌入輸入的 SHA-256。"""
    table, _ = _store(embedder.dim)
    fingerprint = getattr(embedder, "fingerprint", None)
    if embedder.dim == 768 and (fingerprint is None or getattr(embedder, "side", None) != "document"):
        raise IncompatibleIndex("768 維索引必須用帶嵌入指紋的文件端嵌入器建立（查詢前綴不能用來建索引）")
    with conn.transaction():
        index_id = conn.execute(
            """INSERT INTO index_versions (label, embedding_model, embedding_dim, notes, embedding_fingerprint)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (label, embedder.model, embedder.dim, notes, Jsonb(fingerprint) if fingerprint else None),
        ).fetchone()["id"]
        conn.execute(
            """INSERT INTO index_version_documents (index_version_id, document_version_id)
               SELECT DISTINCT %s, document_version_id FROM usable_chunks""",
            (index_id,),
        )
        rows = conn.execute(
            f"SELECT chunk_id, search_text, content_sha256 FROM usable_chunks WHERE {_INDEXABLE} ORDER BY chunk_id"
        ).fetchall()
        for start in range(0, len(rows), batch):
            part = rows[start:start + batch]
            vectors = embedder.embed([r["search_text"] for r in part])
            if len(vectors) != len(part):
                raise RetrievalError("嵌入模型回傳的向量數量與輸入不符")
            with conn.cursor() as cur:
                if table == "chunk_embeddings":
                    cur.executemany(
                        """INSERT INTO chunk_embeddings (index_version_id, chunk_id, embedding, content_sha256)
                           VALUES (%s, %s, %s::vector, %s)""",
                        [(index_id, r["chunk_id"], _vector_literal(v, embedder.dim), r["content_sha256"])
                         for r, v in zip(part, vectors)],
                    )
                else:
                    cur.executemany(
                        """INSERT INTO chunk_embeddings_v768 (index_version_id, chunk_id, embedding,
                                                              content_sha256, input_sha256)
                           VALUES (%s, %s, %s::vector, %s, %s)""",
                        [(index_id, r["chunk_id"], _vector_literal(v, embedder.dim), r["content_sha256"],
                          embedder.input_sha256(r["search_text"])) for r, v in zip(part, vectors)],
                    )
    return {"label": label, "index_version_id": index_id, "embedded": len(rows),
            "embedding_model": embedder.model, "embedding_dim": embedder.dim, "status": "building",
            "embedding_fingerprint": fingerprint}


def index_coverage(conn: psycopg.Connection, label: str) -> dict:
    """可用片段中，有幾筆在此索引有內容相符的向量。缺的片段代表索引尚未就緒。"""
    row = conn.execute("SELECT embedding_dim FROM index_versions WHERE label = %s", (label,)).fetchone()
    table, _ = _store(row["embedding_dim"] if row else 1024)
    return conn.execute(
        f"""SELECT count(*) AS usable,
                  count(ce.chunk_id) AS embedded,
                  count(*) - count(ce.chunk_id) AS missing
             FROM usable_chunks u
             LEFT JOIN {table} ce
               ON ce.chunk_id = u.chunk_id AND ce.content_sha256 = u.content_sha256
              AND ce.index_version_id = (SELECT id FROM index_versions WHERE label = %s)
            WHERE {_INDEXABLE.replace("attributes", "u.attributes")}""",
        (label,),
    ).fetchone()


def activate_index(conn: psycopg.Connection, label: str) -> dict:
    """新索引涵蓋全部可用片段才切換；同一交易內舊 active → retired。"""
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('index_versions'))")
        target = conn.execute("SELECT id, status FROM index_versions WHERE label = %s", (label,)).fetchone()
        if target is None:
            raise IndexNotReady(f"找不到索引 {label}")
        if target["status"] != "building":
            raise IndexNotReady(f"索引 {label} 狀態是 {target['status']}，只能啟用 building")
        cov = index_coverage(conn, label)
        if cov["usable"] == 0 or cov["missing"]:
            raise IndexNotReady(f"索引 {label} 尚未就緒：可用 {cov['usable']}、缺 {cov['missing']} 筆向量")
        old = conn.execute(
            """UPDATE index_versions SET status = 'retired', retired_at = now()
                WHERE status = 'active' RETURNING label"""
        ).fetchone()
        conn.execute("UPDATE index_versions SET status = 'active', activated_at = now() WHERE id = %s",
                     (target["id"],))
    return {"activated": label, "retired": old["label"] if old else None, **cov}


# ---------------------------------------------------------------------------
# 搜尋與再驗證
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Hit:
    rank: int
    chunk_id: int
    chunk_key: str
    doc_key: str
    version_label: str
    page_start: int | None
    page_end: int | None
    content_sha256: str
    index_label: str
    distance: float

    @property
    def score(self) -> float:
        return 1.0 - self.distance  # cosine 相似度


def search(conn: psycopg.Connection, query: str, role: str, embedder: Embedder, k: int = 5) -> list[Hit]:
    """在使用者可用的候選集合中做向量搜尋；回傳前 k 名（可能少於 k）。"""
    if not query.strip():
        raise RetrievalError("查詢不可為空")
    _visibility(role)
    index = active_index(conn)
    if index is None:
        raise IndexNotReady("沒有 active 索引")
    check_compatible(index, embedder)
    return _vector_search(conn, embedder.embed([query])[0], role, index, k)


def _vector_search(conn: psycopg.Connection, vector: Sequence[float], role: str, index: dict, k: int) -> list[Hit]:
    """以查詢向量在 active 索引（依維度選 view）＋角色 visibility 內取前 k 名。"""
    visibility = _visibility(role)
    _, view = _store(index["embedding_dim"])
    qvec = _vector_literal(vector, index["embedding_dim"])
    rows = conn.execute(
        f"""SELECT chunk_id, chunk_key, doc_key, version_label, page_start, page_end, content_sha256,
                  index_label, embedding <=> %s::vector AS distance
             FROM {view}
            WHERE index_version_id = %s AND visibility = ANY(%s)
            ORDER BY distance, chunk_id
            LIMIT %s""",
        (qvec, index["id"], visibility, k),
    ).fetchall()
    return [Hit(rank=i, **r) for i, r in enumerate(rows, start=1)]


HYBRID_VERSION = "hybrid-v3"   # v3：場合詞只降權（規則在 qa.keyword_plan）
HYBRID_EACH = 50      # 兩路各取幾段再合併（同 RTU-M4）
RRF_K = 60            # RRF 常數：名次分數 1／(RRF_K＋名次)
WEAK_WEIGHT = 0.2     # hybrid-v2：泛稱詞（品類詞）在關鍵詞排序裡的權重（核心詞是 1）


def _keyword_search(conn: psycopg.Connection, phrases: Sequence[str], vector: Sequence[float], role: str,
                    index: dict, k: int, weak_phrases: Sequence[str] = ()) -> list[Hit]:
    """關鍵詞路：任一核心說法（整串照順序，phraseto_tsquery）出現在 search_text 才算候選。

    排序是 ts_rank(核心) ＋ WEAK_WEIGHT × ts_rank(泛稱)：泛稱詞只能幫已命中核心詞的片段往前，
    只命中泛稱詞的片段不算關鍵詞命中（hybrid-v2，2026-10-27）。
    distance 照樣算查詢向量的 cosine 距離，讓合併後每段的分數意義和向量路一致。沒有核心說法就回空清單。"""
    phrases = [p for p in dict.fromkeys(p.strip() for p in phrases) if p]
    weak = [p for p in dict.fromkeys(p.strip() for p in weak_phrases) if p and p not in phrases]
    if not phrases:
        return []
    visibility = _visibility(role)
    _, view = _store(index["embedding_dim"])
    qvec = _vector_literal(vector, index["embedding_dim"])
    core_q = " || ".join(["phraseto_tsquery('english', %s)"] * len(phrases))
    weak_q = " || ".join(["phraseto_tsquery('english', %s)"] * len(weak)) if weak else "NULL::tsquery"
    rows = conn.execute(
        f"""WITH q AS (SELECT ({core_q}) AS core, ({weak_q}) AS weak),
                 c AS (SELECT s.*, to_tsvector('english', s.search_text) AS tsv
                         FROM {view} s
                        WHERE s.index_version_id = %s AND s.visibility = ANY(%s))
            SELECT chunk_id, chunk_key, doc_key, version_label, page_start, page_end, content_sha256,
                   index_label, embedding <=> %s::vector AS distance
              FROM c, q
             WHERE c.tsv @@ q.core
             ORDER BY ts_rank(c.tsv, q.core) + %s * coalesce(ts_rank(c.tsv, q.weak), 0) DESC, chunk_id
             LIMIT %s""",
        (*phrases, *weak, index["id"], visibility, qvec, WEAK_WEIGHT, k),
    ).fetchall()
    return [Hit(rank=i, **r) for i, r in enumerate(rows, start=1)]


def rrf_merge(vector_hits: Sequence[Hit], keyword_hits: Sequence[Hit], k: int) -> tuple[list[Hit], list[dict]]:
    """兩路名次以 RRF 合併，取前 k 段並重編名次；同分依向量名次、再依 chunk_id。

    回傳（合併後的命中, 每段的追溯列 {unit_id, vector_rank, keyword_rank, rrf}）——日誌用，看每段從哪一路進池。"""
    by_id: dict[int, Hit] = {}
    ranks: dict[int, dict] = {}
    for route, hits in (("vector_rank", vector_hits), ("keyword_rank", keyword_hits)):
        for h in hits:
            by_id.setdefault(h.chunk_id, h)
            ranks.setdefault(h.chunk_id, {"vector_rank": None, "keyword_rank": None})[route] = h.rank
    score = {cid: sum(1.0 / (RRF_K + r) for r in rk.values() if r is not None) for cid, rk in ranks.items()}
    order = sorted(score, key=lambda cid: (-score[cid], ranks[cid]["vector_rank"] or 10**9, cid))[:k]
    merged = [replace(by_id[cid], rank=i) for i, cid in enumerate(order, start=1)]
    trace = [{"unit_id": by_id[cid].chunk_key, **ranks[cid], "rrf": round(score[cid], 6)} for cid in order]
    return merged, trace


def hybrid_search(conn: psycopg.Connection, query: str, phrases: Sequence[str], role: str, embedder: Embedder,
                  k: int = 5, each: int = HYBRID_EACH, weak_phrases: Sequence[str] = ()) -> tuple[list[Hit], dict]:
    """兩路檢索：向量（中文原句，只編碼一次）＋關鍵詞（英文說法），各取 max(each, k) 段後 RRF 合併取前 k。

    weak_phrases 是泛稱詞（只降權、不單獨成為命中）。回傳（命中, 追溯 {version, phrases, weak_phrases, vector_count, keyword_count, merged}）。
    沒有說法時只有向量路：RRF 分數隨向量名次遞減，名次和 search() 相同。"""
    if not query.strip():
        raise RetrievalError("查詢不可為空")
    _visibility(role)
    index = active_index(conn)
    if index is None:
        raise IndexNotReady("沒有 active 索引")
    check_compatible(index, embedder)
    vector = embedder.embed([query])[0]
    pool = max(each, k)
    vector_hits = _vector_search(conn, vector, role, index, pool)
    keyword_hits = _keyword_search(conn, phrases, vector, role, index, pool, weak_phrases)
    # 2026-10-30（W01 召回）：先合併、去重、排完全部，再截前 k；截掉的也留追溯與淘汰原因（排序規則不變）
    full, full_rows = rrf_merge(vector_hits, keyword_hits, len(vector_hits) + len(keyword_hits))
    merged, rows = full[:k], full_rows[:k]
    cutoff = rows[-1]["rrf"] if rows else None
    cut = [{**r, "fused_rank": i, "reason": f"融合分數 {r['rrf']} 低於第 {k} 名的 {cutoff}，候選池截斷"}
           for i, r in enumerate(full_rows[k:], start=k + 1)]
    return merged, {"version": HYBRID_VERSION, "phrases": list(dict.fromkeys(phrases)),
                    "weak_phrases": list(dict.fromkeys(weak_phrases)),
                    "vector_count": len(vector_hits), "keyword_count": len(keyword_hits), "merged": rows,
                    "cut": cut}


# 支持單位欄位（A 匯出、存在 attributes.support_unit）攤平成 B／C 讀的扁平 dict（contract-v1.2 metadata）；
# text、text_sha256、source_id、version_label 以資料庫為準，不用匯出檔的值。
def unit_view(row: dict) -> dict:
    su = dict(row.get("support_unit") or {})
    view = {**su, **{k: v for k, v in row.items() if k != "support_unit"}}
    view.update(unit_id=row["chunk_key"], text=row["quote_text"], source_id=row["doc_key"],
                text_sha256=hashlib.sha256(row["quote_text"].encode("utf-8")).hexdigest(),
                eligibility=su.get("eligibility"),
                section_original=su.get("section_original") or row.get("section"),
                locator_primary=su.get("locator_primary") or _locator_of(row),
                conditions=list(su.get("conditions") or []), product_mentions=list(su.get("product_mentions") or []),
                links=su.get("links") or {}, support_unit_present=bool(su))
    for k in _UNIT_META_FIELDS:   # 沒有 A 單位的片段也帶齊契約欄位（值為 None），B／C 不必猜鍵是否存在
        view.setdefault(k, None)
    view["content_role"], fixed = effective_content_role(view["unit_id"], view["content_role"], view["product_mentions"])
    if fixed:
        view["content_role_inherited"] = ROLE_PRODUCT_CLAIM   # 追溯：資料上的原標籤
    return view


# split-role-v1（2026-11-09，fix-metrics，測試組 H06）：拆分單位（#a1、#a2…）沿用了父段落的「商品宣稱」，
# 但拆出來的這段已經沒有商品句（product_mentions 為空）。例：PR015_h0139 父段最後一句推薦商品，
# 拆出的 PR015_h0139#a1 只剩「別疊太多層、先薄擦一層、等定型、需要再補」，仍標商品宣稱，
# 提示第 6 條（商品宣稱的效果不能說成一般效果）就讓模型不用這段。資料不改，讀取時更正。
SPLIT_ROLE_VERSION = "split-role-v1"
ROLE_PRODUCT_CLAIM = "商品宣稱"


def effective_content_role(unit_id: str, role: str | None, product_mentions) -> tuple[str | None, bool]:
    """回傳（實際採用的內容角色, 是否有更正）。

    只更正一種情況：拆分單位（編號含「#a」）、標商品宣稱、而且本身沒有任何商品指涉 → 角色改成 None（不標）。
    其他（合併單位 #m、拆句 #s、一般段落、有商品名的拆分單位）一律照資料。"""
    if role == ROLE_PRODUCT_CLAIM and "#a" in (unit_id or "") and not product_mentions:
        return None, True
    return role, False


_UNIT_META_FIELDS = ("source_version", "export_sha256", "locator_secondary", "section_search", "content_role",
                     "support_scope", "tokens_cl100k", "search_text_base_sha256")


def _locator_of(row: dict) -> str | None:
    ps, pe = row.get("page_start"), row.get("page_end")
    if ps is not None:
        return f"p.{ps}" if pe in (None, ps) else f"p.{ps}–{pe}"
    return row.get("html_locator")


# neighbor-v1（2026-11-03，N10）：網頁文章常把一節拆成「說明段」＋下一段「Get the look: 做法」。
# 說明段排在前面時，做法段常因語意不同進不了候選池（N10：h0178 第 1、h0179 不在池內，回答缺底妝步驟）。
# 規則：前 NEIGHBOR_TOP 名、編號是 <文件>_h<4 位數> 的段落，往後看最多 NEIGHBOR_LOOKAHEAD 段，
# 跳過只有社群嵌入字樣的段落；第一個有內容的段落以「Get the look」開頭才補進來（排在錨段後面，池子大小不變）。
NEIGHBOR_VERSION = "neighbor-v1"
NEIGHBOR_TOP = 3
NEIGHBOR_LOOKAHEAD = 3
_RE_HTML_KEY = re.compile(r"^(?P<doc>[A-Za-z0-9]+)_h(?P<n>\d{4})$")
_RE_EMBED_ONLY = re.compile(r"^\s*View this post on Instagram", re.I)
_RE_HOW_TO = re.compile(r"^\s*Get the look\s*:", re.I)


def _next_keys(chunk_key: str) -> list[str]:
    """錨段後面依序可能的續段編號（不含錨段本身）；編號格式不符回空清單。"""
    m = _RE_HTML_KEY.match(chunk_key)
    if not m:
        return []
    n = int(m["n"])
    return [f"{m['doc']}_h{n + i:04d}" for i in range(1, NEIGHBOR_LOOKAHEAD + 1)]


def neighbor_fill(conn: psycopg.Connection, hits: Sequence[Hit], role: str, query: str, embedder: Embedder,
                  k: int) -> tuple[list[Hit], list[dict]]:
    """前幾名錨段若有「Get the look」續段、續段又不在池內，就把續段插在錨段後面，再截回 k 段。

    續段一樣只在角色可用、active 索引內找；distance 用查詢向量重算（只在真的有續段時才編碼一次）。
    回傳（新的命中清單, 追溯 [{anchor, added, anchor_rank}]）。"""
    have = {h.chunk_key for h in hits}
    wanted: dict[str, list[str]] = {}
    for h in hits[:NEIGHBOR_TOP]:
        keys = _next_keys(h.chunk_key)
        if keys:
            wanted[h.chunk_key] = keys
    if not wanted:
        return list(hits), []
    all_keys = [x for keys in wanted.values() for x in keys]
    texts = {r["chunk_key"]: r["quote_text"] for r in conn.execute(
        "SELECT chunk_key, quote_text FROM usable_chunks WHERE chunk_key = ANY(%s) AND visibility = ANY(%s)",
        (all_keys, _visibility(role)))}
    picks: dict[str, str] = {}
    for anchor, keys in wanted.items():
        for key in keys:
            text = texts.get(key)
            if text is None or _RE_EMBED_ONLY.match(text):
                continue                      # 不存在／不可用／只是社群嵌入 → 再往後看
            if _RE_HOW_TO.match(text) and key not in have:
                picks[anchor] = key
            break                             # 第一個有內容的段落不是做法段就停
    if not picks:
        return list(hits), []
    index = active_index(conn)
    _, view = _store(index["embedding_dim"])
    qvec = _vector_literal(embedder.embed([query])[0], index["embedding_dim"])
    rows = {r["chunk_key"]: r for r in conn.execute(
        f"""SELECT chunk_id, chunk_key, doc_key, version_label, page_start, page_end, content_sha256,
                  index_label, embedding <=> %s::vector AS distance
             FROM {view}
            WHERE index_version_id = %s AND visibility = ANY(%s) AND chunk_key = ANY(%s)""",
        (qvec, index["id"], _visibility(role), list(picks.values()))).fetchall()}
    out, trace = [], []
    for h in hits:
        out.append(h)
        key = picks.get(h.chunk_key)
        if key in rows and key not in {x.chunk_key for x in out}:
            out.append(Hit(rank=0, **rows[key]))
            trace.append({"anchor": h.chunk_key, "anchor_rank": h.rank, "added": key,
                          "cosine": round(1 - rows[key]["distance"], 4)})
    out = [replace(h, rank=i) for i, h in enumerate(out[:k], start=1)]
    return out, trace
# 核心邏輯：只補「說明段→做法段」這種固定版型（以原文開頭判斷，不用模型）；池子大小不變，被擠掉的是最後一名。


class CandidateStore:
    """給 B 的 retrieval_policy.retrieve_candidates 用的資料庫存取（contract-v1 store）。

    vector_search(向量, k)／search(查詢, k, embedder) 只在角色可用、active 索引中內容相符的片段裡搜尋；
    建構時給的 embedder 用來比對索引相容性（模型、維度、嵌入指紋）。units() 取原文與出處。
    交出的每個命中都記下來，管線據此確認候選確實來自資料庫、原文沒有被改寫。"""

    def __init__(self, conn: psycopg.Connection, role: str, embedder: Embedder | None = None,
                 keyword_phrases: Sequence[str] = (), weak_phrases: Sequence[str] = ()):
        """keyword_phrases：關鍵詞路的核心英文說法；weak_phrases：只降權的泛稱詞（hybrid-v2）。
        沒有核心說法就只有向量路，和原本行為相同。"""
        self.conn, self.role, self.embedder = conn, role, embedder
        self.keyword_phrases = list(keyword_phrases)
        self.weak_phrases = list(weak_phrases)
        self.hybrid_trace: dict | None = None    # 兩路檢索的追溯（只在有說法時才有）
        self.neighbor_trace: list[dict] = []      # neighbor-v1：補進來的續段（錨段、名次、cosine）
        self._returned: dict[int, Hit] = {}

    def vector_search(self, query_vector: Sequence[float], k: int) -> list[dict]:
        """B 的 CandidateStore 介面：回傳排序後的列 {unit_id, text, score, tokens, meta}。"""
        index = active_index(self.conn)
        if index is None:
            raise IndexNotReady("沒有 active 索引")
        if self.embedder is None:
            raise RetrievalError("CandidateStore 需要查詢嵌入器才能確認索引相容")
        check_compatible(index, self.embedder)
        hits = _vector_search(self.conn, query_vector, self.role, index, k)
        for h in hits:
            self._returned.setdefault(h.chunk_id, h)
        views = {u["unit_id"]: u for u in map(unit_view, context_chunks(self.conn, hits))}
        out = []
        for h in hits:
            u = views.get(h.chunk_key)
            if u is None:   # 搜尋與取原文之間被下架：不交出，之後的 revalidate 也會擋
                continue
            out.append({"unit_id": h.chunk_key, "text": u["text"], "score": h.score,
                        "tokens": u.get("tokens_cl100k"), "meta": u})
        return out

    def search(self, query: str, k: int, embedder: Embedder) -> list[Hit]:
        """B 的 store.search 介面：有英文說法時走兩路 RRF（hybrid_search），否則照舊只走向量。"""
        if self.keyword_phrases:
            hits, self.hybrid_trace = hybrid_search(self.conn, query, self.keyword_phrases, self.role, embedder, k=k,
                                                    weak_phrases=self.weak_phrases)
        else:
            hits = search(self.conn, query, self.role, embedder, k=k)
        hits, self.neighbor_trace = neighbor_fill(self.conn, hits, self.role, query, embedder, k)
        # multi-need-v1（2026-11-07）：同一個 store 會被同一題的子查詢再搜幾次；已交出過的片段回傳「第一次交出的那一份」，
        # units() 的身分檢查（只能取本次搜尋交出的片段）才能照舊成立。第一次搜尋時行為與原本完全相同。
        return [self._returned.setdefault(h.chunk_id, h) for h in hits]

    def units(self, hits: Iterable[Hit]) -> list[dict]:
        """片段原文、檢索文字與出處（含 support_unit 屬性）；只接受本 store 交出的命中。"""
        hits = list(hits)
        if any(self._returned.get(h.chunk_id) is not h for h in hits):
            raise RetrievalError("只能取本次搜尋交出的片段")
        rows = [unit_view(r) for r in context_chunks(self.conn, hits)]
        texts = {r["chunk_id"]: r["search_text"] for r in self.conn.execute(
            "SELECT chunk_id, search_text FROM usable_chunks WHERE chunk_id = ANY(%s)",
            ([h.chunk_id for h in hits],))}
        return [{**r, "search_text": texts.get(r["chunk_id"])} for r in rows]

    def units_by_id(self, unit_ids: Iterable[str]) -> dict[str, dict]:
        """contract-v1.2 固定映射：{unit_id: 單位檢視}，由同一批資料庫列取回；只含本 store 交出的單位。

        單位已不可用（搜尋後被下架）就不在結果裡；呼叫端以 text_sha256 比對各階段拿到的是同一份原文。"""
        by_key = {h.chunk_key: h for h in self._returned.values()}
        hits = [by_key[u] for u in dict.fromkeys(unit_ids) if u in by_key]
        return {r["chunk_key"]: unit_view(r) for r in context_chunks(self.conn, hits)}

    def returned_hits(self) -> list[Hit]:
        return list(self._returned.values())


def revalidate(conn: psycopg.Connection, hits: Iterable[Hit], role: str) -> tuple[list[Hit], dict[int, str]]:
    """送進模型前重查：仍可用、角色可見、內容未變。回傳（通過的命中, {chunk_id: 被擋原因}）。"""
    hits = list(hits)
    if not hits:
        return [], {}
    visibility = _visibility(role)
    current = {
        r["chunk_id"]: r for r in conn.execute(
            "SELECT chunk_id, visibility, content_sha256 FROM usable_chunks WHERE chunk_id = ANY(%s)",
            ([h.chunk_id for h in hits],),
        )
    }
    passed, blocked = [], {}
    for h in hits:
        row = current.get(h.chunk_id)
        if row is None:
            blocked[h.chunk_id] = "no_longer_usable"
        elif row["visibility"] not in visibility:
            blocked[h.chunk_id] = "not_visible_to_role"
        elif row["content_sha256"] != h.content_sha256:
            blocked[h.chunk_id] = "content_changed"
        else:
            passed.append(h)
    return passed, blocked


def context_chunks(conn: psycopg.Connection, hits: Iterable[Hit]) -> list[dict]:
    """取已通過 revalidate 的片段原文與出處，作為給模型的上下文（只含這些片段）。"""
    ids = [h.chunk_id for h in hits]
    if not ids:
        return []
    rows = conn.execute(
        """SELECT chunk_id, chunk_key, doc_key, version_label, document_version_id, title, page_start, page_end,
                  html_locator, section, quote_text, content_sha256, attributes -> 'citation' AS citation,
                  attributes -> 'support_unit' AS support_unit, attributes ->> 'source_url' AS source_url
             FROM usable_chunks WHERE chunk_id = ANY(%s)""",
        (ids,),
    ).fetchall()
    order = {cid: i for i, cid in enumerate(ids)}
    return sorted(rows, key=lambda r: order[r["chunk_id"]])


def record_hits(conn: psycopg.Connection, request_id, hits: Sequence[Hit], blocked: dict[int, str]) -> None:
    """寫入 retrieval_hits：被再驗證擋下的也保留（passed_filter = false＋原因），方便稽核。"""
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO retrieval_hits (request_id, retriever, rank, chunk_id, score, passed_filter,
                                           filter_reason, detail)
               VALUES (%s, 'vector', %s, %s, %s, %s, %s, %s)""",
            [(request_id, h.rank, h.chunk_id, h.score, h.chunk_id not in blocked, blocked.get(h.chunk_id),
              Jsonb({"index_label": h.index_label, "version_label": h.version_label,
                     "content_sha256": h.content_sha256})) for h in hits],
        )


# ---------------------------------------------------------------------------
# 指令列：建索引（可只用快取）、啟用索引
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> None:
    """python -m app.retrieval build-index --label L [--cache 快取.jsonl] [--cache-only] [--activate]
       python -m app.retrieval activate --label L

    嵌入模型設定讀 EMBEDDING_*（model_adapters）。--cache-only：只用快取，有任何未命中就整批失敗、不呼叫模型
    （E 已產生快取時用；未命中代表資料庫 search_text 與快取輸入不是逐字相同）。"""
    ap = argparse.ArgumentParser(prog="python -m app.retrieval")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build-index")
    b.add_argument("--label", required=True)
    b.add_argument("--cache", type=Path)
    b.add_argument("--cache-only", action="store_true")
    b.add_argument("--activate", action="store_true")
    b.add_argument("--notes")
    a = sub.add_parser("activate")
    a.add_argument("--label", required=True)
    args = ap.parse_args(argv)
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("請設定 DATABASE_URL")
    from psycopg.rows import dict_row
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
        try:
            if args.cmd == "activate":
                out = activate_index(conn, args.label)
            else:
                if args.cache_only and not args.cache:
                    sys.exit("--cache-only 需要 --cache")
                emb = get_document_embedder()
                cache = model_adapters.EmbeddingCache.load(args.cache) if args.cache else None
                if cache is not None:
                    emb = model_adapters.CachedEmbedder(emb, cache, allow_model_calls=not args.cache_only)
                out = build_index(conn, args.label, emb, notes=args.notes)
                if cache is not None:
                    out["cache"] = {"path": str(args.cache), "hits": emb.hits, "misses": emb.misses}
                    if emb.misses and not args.cache_only:
                        cache.save(args.cache)
                if args.activate:
                    out["activate"] = activate_index(conn, args.label)
        except (RetrievalError, model_adapters.AdapterCallFailed, psycopg.Error) as exc:
            sys.exit(f"失敗：{exc}")
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
