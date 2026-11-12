"""DEC-11 嵌入模型比較：以 S4 的 5 題「中文問句查英文證據」比較候選嵌入模型的召回。

  python -m app.embed_compare --root F:\\LipMatch_AI [--models text-embedding-3-small text-embedding-3-large]
                              [--dim 1024] [--out ../data/review/s5/embed_compare.json]

用途與界線：
- 只用來「選嵌入模型」，不是檢索驗收：語料是 data/processed/rag_ready 的全部片段（**尚未人工核准**），
  不寫入資料庫、不建索引、不產生任何回答；正式檢索仍只用 usable_chunks。
- 預期片段是 S4 的開發者草稿（未經人工核對）；結果只能比較模型之間的相對差異。
- 會把片段文字與問句送到 OpenAI Embeddings API（金鑰取自環境變數 OPENAI_API_KEY，不寫入任何檔案或日誌）。
  向量快取在 --out 同目錄（data/review 不進 Git）。輸出只含片段 ID 與分數，不含原文。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

QUERIES = Path(__file__).resolve().parent.parent / "eval" / "s4_known_evidence.json"
OPENAI_URL = "https://api.openai.com/v1/embeddings"

# S4 預期片段在 ETL 段落接續修正後的對應（block_id 前移）。屬開發者對照，需人工確認後再改回 S4 檔案。
EXPECTED_ALIASES = {"AR015_p0001_b014": "AR015_p0001_b013"}


class OpenAIEmbedder:
    """OpenAI Embeddings API（標準函式庫 HTTP）。記錄每次呼叫的 token、耗時、重試與錯誤。"""

    provider = "openai"

    def __init__(self, model: str, dim: int, api_key: str, timeout_s: float = 60.0, max_retries: int = 1):
        self.model, self.dim = model, dim
        self._key, self.timeout_s, self.max_retries = api_key, timeout_s, max_retries
        self.calls: list[dict] = []

    # 嵌入一批文字；429／5xx／逾時重試有限次數，其他錯誤直接拋出。回傳與輸入同順序的向量。
    def embed(self, texts: list[str]) -> list[list[float]]:
        body = json.dumps({"model": self.model, "input": texts, "dimensions": self.dim}).encode("utf-8")
        for attempt in range(1, self.max_retries + 2):
            t0 = time.perf_counter()
            req = urllib.request.Request(OPENAI_URL, data=body, method="POST", headers={
                "Content-Type": "application/json", "Authorization": f"Bearer {self._key}"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                transient = exc.code == 429 or exc.code >= 500
                self._log(attempt, t0, len(texts), None, f"http_{exc.code}")
                if transient and attempt <= self.max_retries:
                    time.sleep(2 * attempt)
                    continue
                raise RuntimeError(f"OpenAI Embeddings HTTP {exc.code}") from None
            except (urllib.error.URLError, TimeoutError) as exc:
                self._log(attempt, t0, len(texts), None, type(exc).__name__)
                if attempt <= self.max_retries:
                    time.sleep(2 * attempt)
                    continue
                raise RuntimeError("無法連線到 OpenAI Embeddings") from None
            usage = (payload.get("usage") or {}).get("prompt_tokens")
            self._log(attempt, t0, len(texts), usage, None)
            data = sorted(payload["data"], key=lambda d: d["index"])
            vecs = [d["embedding"] for d in data]
            if len(vecs) != len(texts) or any(len(v) != self.dim for v in vecs):
                raise RuntimeError("嵌入結果數量或維度不符")
            return vecs
        raise RuntimeError("重試次數用盡")
    # 核心邏輯：每次嘗試都寫一筆紀錄（token 沒回報就記 None）；金鑰只放在請求標頭，不出現在紀錄或錯誤訊息。

    # 附加一筆呼叫紀錄。
    def _log(self, attempt: int, t0: float, n: int, tokens: int | None, error: str | None) -> None:
        self.calls.append({"model": self.model, "attempt": attempt, "inputs": n, "input_tokens": tokens,
                           "latency_ms": int((time.perf_counter() - t0) * 1000), "error": error})


# 讀取 rag_ready 的全部片段（block_id、合併來源 block_ids、檢索用文字）。
def load_corpus(root: Path) -> list[dict]:
    rows = []
    for path in sorted((root / "data" / "processed" / "rag_ready").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                rows.append({"block_id": r["block_id"], "block_ids": r.get("block_ids") or [r["block_id"]],
                             "doc_id": r["doc_id"], "text": r["text"]})
    return rows


# 以快取避免重複付費：快取鍵 = 模型＋維度＋文字 SHA-256。
def embed_cached(emb: OpenAIEmbedder, texts: list[str], cache_path: Path, batch: int = 64) -> list[list[float]]:
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    keys = [hashlib.sha256(f"{emb.model}|{emb.dim}|{t}".encode()).hexdigest() for t in texts]
    todo = [i for i, k in enumerate(keys) if k not in cache]
    for start in range(0, len(todo), batch):
        part = todo[start:start + batch]
        for i, vec in zip(part, emb.embed([texts[i] for i in part])):
            cache[keys[i]] = vec
    cache_path.write_text(json.dumps(cache), encoding="utf-8")
    return [cache[k] for k in keys]


# 餘弦相似度（向量未必已正規化）。
def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


# 片段是否對應某個預期 block_id（含合併後的 block_ids 與別名）。
def matches(chunk: dict, key: str) -> bool:
    key = EXPECTED_ALIASES.get(key, key)
    return key == chunk["block_id"] or key in chunk["block_ids"]


# 計算一個模型在全部題目上的名次、hit@k、MRR。
def evaluate(corpus: list[dict], cvecs: list[list[float]], spec: dict, qvecs: list[list[float]], k: int) -> dict:
    results = []
    for q, qv in zip(spec["queries"], qvecs):
        ranked = sorted(((cosine(qv, cv), i) for i, cv in enumerate(cvecs)), reverse=True)
        order = [corpus[i] for _, i in ranked]
        score = {corpus[i]["block_id"]: s for s, i in ranked}

        # 某個預期 block_id 在全部語料中的名次（1 起算）。
        def rank_of(key: str) -> int | None:
            return next((n for n, c in enumerate(order, start=1) if matches(c, key)), None)

        exp = {key: rank_of(key) for key in q["expected"]}
        best = min((r for r in exp.values() if r is not None), default=None)
        results.append({"id": q["id"], "lang": q["lang"], "expected_ranks": exp,
                        "also_relevant_ranks": {key: rank_of(key) for key in q.get("also_relevant", [])},
                        "hit_at_k": best is not None and best <= k, "first_expected_rank": best,
                        "top_k": [{"rank": n, "block_id": c["block_id"], "score": round(score[c["block_id"]], 4)}
                                  for n, c in enumerate(order[:k], start=1)]})
    hits = sum(r["hit_at_k"] for r in results)
    mrr = sum(1 / r["first_expected_rank"] for r in results if r["first_expected_rank"]) / len(results)
    return {"recall_at_k": f"{hits}/{len(results)}", "mrr": round(mrr, 4), "results": results}
# 核心邏輯：每題對整個語料排序，記錄每個預期片段的實際名次（不只看前 k）；
# recall@k = 任一預期片段在前 k 名的題數；MRR 用第一個預期片段的名次。


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m app.embed_compare")
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--models", nargs="+", default=["text-embedding-3-small", "text-embedding-3-large"])
    ap.add_argument("--dim", type=int, default=1024)
    ap.add_argument("--queries", type=Path, default=QUERIES)
    ap.add_argument("--out", type=Path, default=Path("../data/review/s5/embed_compare.json"))
    args = ap.parse_args(argv)
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        sys.exit("請設定 OPENAI_API_KEY（只從環境變數讀取）")
    spec = json.loads(args.queries.read_text(encoding="utf-8"))
    k = spec["k"]
    corpus = load_corpus(args.root)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    report = {"run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "k": k, "dim": args.dim,
              "corpus": {"chunks": len(corpus), "docs": sorted({c["doc_id"] for c in corpus}),
                         "review_status": "未人工核准（只用於選型，不是檢索驗收）"},
              "expected_aliases": EXPECTED_ALIASES, "models": {}}
    for model in args.models:
        emb = OpenAIEmbedder(model, args.dim, key)
        t0 = time.perf_counter()
        cache = args.out.parent / f"embcache_{model}_{args.dim}.json"
        cvecs = embed_cached(emb, [c["text"] for c in corpus], cache)
        corpus_s = time.perf_counter() - t0
        qt = time.perf_counter()
        qvecs = emb.embed([q["query"] for q in spec["queries"]])
        query_ms = int((time.perf_counter() - qt) * 1000)
        tokens = [c["input_tokens"] for c in emb.calls if c["error"] is None]
        report["models"][model] = {
            **evaluate(corpus, cvecs, spec, qvecs, k),
            "corpus_embed_seconds": round(corpus_s, 1), "query_batch_ms": query_ms,
            "api_calls": len(emb.calls), "api_errors": sum(c["error"] is not None for c in emb.calls),
            "input_tokens_this_run": None if any(t is None for t in tokens) else sum(tokens),
        }
        m = report["models"][model]
        print(f"{model}@{args.dim}: recall@{k}={m['recall_at_k']} MRR={m['mrr']} "
              f"calls={m['api_calls']} tokens={m['input_tokens_this_run']}")
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"→ {args.out}")


if __name__ == "__main__":
    main()
