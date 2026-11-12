"""S4 檢索基準：對已知證據的開發查詢，保存預期與實際召回。

  python -m app.retrieval_bench --role user [--out ../data/review/s4/bench.json]

需要：正式嵌入模型（DEC-11）、已核准並發布的片段、已啟用的索引。任一項缺少都回報阻礙，
不以假向量產生結果。輸出含片段 ID 與分數，不含原文。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from .retrieval import Embedder, RetrievalError, active_index, get_embedder, search

QUERIES = Path(__file__).resolve().parent.parent / "eval" / "s4_known_evidence.json"


def missing_expected(conn: psycopg.Connection, spec: dict) -> dict[str, list[str]]:
    """每題預期片段中，目前不在可檢索集合的有哪些（未核准、未發布、未進索引）。"""
    keys = sorted({k for q in spec["queries"] for k in q["expected"]})
    present = {r["chunk_key"] for r in conn.execute(
        "SELECT chunk_key FROM searchable_chunks WHERE chunk_key = ANY(%s)", (keys,))}
    return {q["id"]: [k for k in q["expected"] if k not in present] for q in spec["queries"]}


def run(conn: psycopg.Connection, embedder: Embedder, role: str, spec: dict) -> dict:
    k = spec["k"]
    index = active_index(conn)
    results = []
    for q in spec["queries"]:
        hits = search(conn, q["query"], role, embedder, k)
        actual = [{"rank": h.rank, "chunk_key": h.chunk_key, "score": round(h.score, 4)} for h in hits]
        ranks = {h.chunk_key: h.rank for h in hits}
        found = {key: ranks.get(key) for key in q["expected"]}
        results.append({"id": q["id"], "lang": q["lang"], "query": q["query"], "expected": q["expected"],
                        "expected_ranks": found, "hit": any(r is not None for r in found.values()),
                        "actual": actual})
    hits_n = sum(r["hit"] for r in results)
    return {
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "role": role, "k": k,
        "index": {"label": index["label"], "embedding_model": index["embedding_model"],
                  "embedding_dim": index["embedding_dim"]} if index else None,
        "recall_at_k": f"{hits_n}/{len(results)}",
        "results": results,
    }


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m app.retrieval_bench")
    ap.add_argument("--role", choices=["user", "admin"], default="user")
    ap.add_argument("--queries", type=Path, default=QUERIES)
    ap.add_argument("--out", type=Path, default=Path("data/review/s4/bench.json"))
    args = ap.parse_args(argv)
    spec = json.loads(args.queries.read_text(encoding="utf-8"))
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("請設定 DATABASE_URL")
    blockers: list[str] = []
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
        try:
            embedder = get_embedder()
        except RetrievalError as exc:
            blockers.append(str(exc))
            embedder = None
        if active_index(conn) is None:
            blockers.append("沒有 active 索引（需先有已核准、已發布的片段並建立索引）")
        missing = {q: keys for q, keys in missing_expected(conn, spec).items() if keys}
        if missing:
            blockers.append(f"預期片段不在可檢索集合（未核准／未發布／未進索引）：{missing}")
        if blockers:
            sys.exit("阻礙，未執行基準：\n- " + "\n- ".join(blockers))
        report = run(conn, embedder, args.role, spec)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"recall@{report['k']} = {report['recall_at_k']} → {args.out}")


if __name__ == "__main__":
    main()
