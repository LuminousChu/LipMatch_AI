"""本機模型接口（修復 D）：xVITA 嵌入走 llama.cpp llama-server 的 /embedding（768 維）。

設定只從環境變數讀取，沒有內建路徑、埠號或前綴；缺任何一項就回報 AdapterNotConfigured（列出缺哪些），
不以假向量代替。生成模型（Gemma，llama.cpp OpenAI 相容 chat）沿用 qa.OpenAICompatibleGenerator，
設定見 qa.QaConfig（GENERATION_PROVIDER=llamacpp）。

  EMBEDDING_PROVIDER=llamacpp
  EMBEDDING_BASE_URL=http://127.0.0.1:8099        # llama-server --embeddings 的位址（由 E 排程啟動）
  EMBEDDING_MODEL=xVITA-Embed-300M-zhTW-F16       # 寫進索引版本的模型名稱
  EMBEDDING_DIM=768
  EMBEDDING_MODEL_SHA256=<GGUF 檔 SHA-256>         # 嵌入指紋：模型檔不同就是不同索引
  EMBEDDING_DOC_PREFIX="title: none | text: "     # 文件端前綴（與研究快取一致才可重用）
  EMBEDDING_QUERY_PREFIX="task: search result | query: "
  EMBEDDING_TIMEOUT_S=120  EMBEDDING_BATCH=16      # 選填

界線：
- 文件與查詢用不同前綴：documents／queries 是同一個連線的兩個視圖，model／dim／fingerprint 相同。
- 快取（EmbeddingCache）只有在「模型檔 SHA-256、前綴、完整嵌入輸入 SHA-256」三者都相同時才重用；
  研究用 seg-v1（1304 段）的向量不會因為文字相近就套到 ETL 片段上。
- 每次 HTTP 呼叫記一筆（筆數、耗時、錯誤）；llama.cpp /embedding 不回報 token，記 None（未知，不記 0）。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence


class AdapterNotConfigured(RuntimeError):
    """必要設定缺漏：不呼叫模型、不產生假向量。"""


class AdapterCallFailed(RuntimeError):
    """模型服務呼叫失敗（連不上、逾時、HTTP 錯誤、回應格式或維度不符）。"""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
_REQUIRED = ("EMBEDDING_BASE_URL", "EMBEDDING_MODEL", "EMBEDDING_DIM", "EMBEDDING_MODEL_SHA256")


@dataclass(frozen=True)
class EmbeddingSettings:
    base_url: str
    model: str
    dim: int
    model_sha256: str
    doc_prefix: str
    query_prefix: str
    timeout_s: float = 120.0
    batch: int = 16

    # 從環境變數建立；provider 不是 llamacpp 或缺必要欄位 → AdapterNotConfigured（訊息列出缺漏，不含任何秘密）。
    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "EmbeddingSettings":
        env = os.environ if env is None else env
        provider = (env.get("EMBEDDING_PROVIDER") or "").strip().lower()
        if provider != "llamacpp":
            raise AdapterNotConfigured(f"EMBEDDING_PROVIDER={provider or '未設定'}（目前只支援 llamacpp）")
        missing = [k for k in _REQUIRED if not (env.get(k) or "").strip()]
        # 前綴可以是空字串，但必須明確設定（未設定 ≠ 空前綴）
        missing += [k for k in ("EMBEDDING_DOC_PREFIX", "EMBEDDING_QUERY_PREFIX") if k not in env]
        if missing:
            raise AdapterNotConfigured("嵌入模型設定不完整，缺少：" + "、".join(missing))
        sha = env["EMBEDDING_MODEL_SHA256"].strip().lower()
        if len(sha) != 64 or any(ch not in "0123456789abcdef" for ch in sha):
            raise AdapterNotConfigured("EMBEDDING_MODEL_SHA256 必須是 64 位十六進位 SHA-256")
        try:
            dim = int(env["EMBEDDING_DIM"])
            timeout_s = float(env.get("EMBEDDING_TIMEOUT_S") or 120)
            batch = int(env.get("EMBEDDING_BATCH") or 16)
        except ValueError:
            raise AdapterNotConfigured("EMBEDDING_DIM／EMBEDDING_TIMEOUT_S／EMBEDDING_BATCH 必須是數字") from None
        if dim <= 0 or batch <= 0 or timeout_s <= 0:
            raise AdapterNotConfigured("EMBEDDING_DIM、EMBEDDING_BATCH、EMBEDDING_TIMEOUT_S 必須大於 0")
        return cls(base_url=env["EMBEDDING_BASE_URL"].strip().rstrip("/"), model=env["EMBEDDING_MODEL"].strip(),
                   dim=dim, model_sha256=sha, doc_prefix=env["EMBEDDING_DOC_PREFIX"],
                   query_prefix=env["EMBEDDING_QUERY_PREFIX"], timeout_s=timeout_s, batch=batch)

    # 寫進 index_versions.embedding_fingerprint 的內容；比對相容性時整份比較。
    def fingerprint(self) -> dict:
        return {"model": self.model, "model_sha256": self.model_sha256, "dim": self.dim,
                "doc_prefix": self.doc_prefix, "query_prefix": self.query_prefix}


# ---------------------------------------------------------------------------
# llama.cpp /embedding 連線
# ---------------------------------------------------------------------------
class LlamaCppEmbeddingClient:
    """POST {base_url}/embedding {"content": [...]}；回傳與輸入同順序的向量。只用標準函式庫。"""

    provider = "llamacpp"

    def __init__(self, settings: EmbeddingSettings, urlopen: Callable | None = None):
        self.settings = settings
        self._urlopen = urlopen or urllib.request.urlopen
        self.calls: list[dict] = []

    # 一批輸入（已含前綴）送一次請求；檢查數量、維度與數值，任何不符都整批失敗。
    def embed_inputs(self, inputs: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(inputs), self.settings.batch):
            out.extend(self._post(list(inputs[start:start + self.settings.batch])))
        return out

    def _post(self, part: list[str]) -> list[list[float]]:
        t0 = time.perf_counter()
        error = None
        try:
            req = urllib.request.Request(f"{self.settings.base_url}/embedding",
                                         data=json.dumps({"content": part}).encode("utf-8"),
                                         headers={"Content-Type": "application/json"}, method="POST")
            try:
                with self._urlopen(req, timeout=self.settings.timeout_s) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                error = f"http_{exc.code}"
                raise AdapterCallFailed(f"嵌入服務回應 HTTP {exc.code}") from None
            except (socket.timeout, TimeoutError):
                error = "timeout"
                raise AdapterCallFailed(f"嵌入服務超過 {self.settings.timeout_s} 秒未回應") from None
            except urllib.error.URLError:
                error = "unreachable"
                raise AdapterCallFailed("無法連線到嵌入服務") from None
            except (ValueError, UnicodeDecodeError):
                error = "bad_response"
                raise AdapterCallFailed("嵌入服務回應不是 JSON") from None
            try:
                vecs = _parse_embedding_payload(payload, len(part), self.settings.dim)
            except AdapterCallFailed:
                error = "bad_response"
                raise
            return vecs
        finally:
            self.calls.append({"stage": "embedding", "model_provider": self.provider,
                               "model_name": self.settings.model, "input_count": len(part),
                               "input_tokens": None, "latency_ms": int((time.perf_counter() - t0) * 1000),
                               "error": error})
    # 核心邏輯：每次 HTTP 請求不論成敗都記一筆；錯誤訊息不含回應本文。


def _parse_embedding_payload(payload, n: int, dim: int) -> list[list[float]]:
    """llama.cpp 回傳 [{"index": i, "embedding": [...] 或 [[...]]}, ...]；依 index 排回輸入順序。"""
    if not isinstance(payload, list) or len(payload) != n:
        raise AdapterCallFailed("嵌入服務回傳的向量數量與輸入不符")
    try:
        rows = sorted(payload, key=lambda r: r["index"])
    except (KeyError, TypeError):
        raise AdapterCallFailed("嵌入服務回應缺少 index") from None
    if [r["index"] for r in rows] != list(range(n)):
        raise AdapterCallFailed("嵌入服務回應的 index 不連續")
    out = []
    for r in rows:
        vec = r.get("embedding")
        if isinstance(vec, list) and vec and isinstance(vec[0], list):
            if len(vec) != 1:  # 多於一列代表伺服器沒有 pooling（逐 token 向量），不能當成片段向量
                raise AdapterCallFailed("嵌入服務回傳逐 token 向量（未設定 pooling）")
            vec = vec[0]
        if not isinstance(vec, list) or len(vec) != dim:
            raise AdapterCallFailed(f"嵌入維度 {len(vec) if isinstance(vec, list) else '?'} 與設定 {dim} 不符")
        if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in vec):
            raise AdapterCallFailed("嵌入向量含非數值")
        out.append([float(x) for x in vec])
    return out


class PrefixedEmbedder:
    """同一連線的文件端或查詢端視圖：embed() 自動加上該端前綴。符合 retrieval.Embedder 介面。"""

    provider = "llamacpp"

    def __init__(self, client: LlamaCppEmbeddingClient, side: str):
        if side not in ("document", "query"):
            raise ValueError(side)
        s = client.settings
        self._client, self.side = client, side
        self.model, self.dim = s.model, s.dim
        self.prefix = s.doc_prefix if side == "document" else s.query_prefix
        self.fingerprint = s.fingerprint()
        self.model_sha256 = s.model_sha256
        self.last_input_tokens = None  # llama.cpp /embedding 不回報 token

    def input_text(self, text: str) -> str:
        return self.prefix + text

    def input_sha256(self, text: str) -> str:
        return sha256_text(self.input_text(text))

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return self._client.embed_inputs([self.input_text(t) for t in texts])

    @property
    def calls(self) -> list[dict]:
        return self._client.calls


@dataclass(frozen=True)
class EmbeddingAdapter:
    documents: PrefixedEmbedder
    queries: PrefixedEmbedder


def embedding_adapter_from_env(env: Mapping[str, str] | None = None,
                               urlopen: Callable | None = None) -> EmbeddingAdapter:
    client = LlamaCppEmbeddingClient(EmbeddingSettings.from_env(env), urlopen=urlopen)
    return EmbeddingAdapter(documents=PrefixedEmbedder(client, "document"),
                            queries=PrefixedEmbedder(client, "query"))


# ---------------------------------------------------------------------------
# 向量快取：模型檔 SHA-256＋前綴＋完整輸入 SHA-256 三者相同才重用
# ---------------------------------------------------------------------------
class EmbeddingCache:
    """JSONL：每行 {"model_sha256", "prefix", "input_sha256", "dim", "vector"}。

    input_sha256 是「前綴＋原文」的 SHA-256；查詢時三個鍵與維度都要相符，任何一項不同就視為未命中。
    檔案只存向量與雜湊，不存原文。"""

    def __init__(self, entries: dict[tuple[str, str, str], list[float]] | None = None):
        self._entries = dict(entries or {})

    @classmethod
    def load(cls, path: Path) -> "EmbeddingCache":
        entries: dict[tuple[str, str, str], list[float]] = {}
        if not path.exists():
            return cls()
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            r = json.loads(line)
            vec = r.get("vector")
            if not isinstance(vec, list) or len(vec) != r.get("dim"):
                raise ValueError(f"{path} 第 {n} 行：向量維度與 dim 不符")
            entries[(r["model_sha256"], r["prefix"], r["input_sha256"])] = [float(x) for x in vec]
        return cls(entries)

    def save(self, path: Path) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            for (model_sha, prefix, input_sha), vec in sorted(self._entries.items()):
                f.write(json.dumps({"model_sha256": model_sha, "prefix": prefix, "input_sha256": input_sha,
                                    "dim": len(vec), "vector": vec}, ensure_ascii=False) + "\n")
        os.replace(tmp, path)

    def get(self, model_sha256: str, prefix: str, text: str, dim: int) -> list[float] | None:
        vec = self._entries.get((model_sha256, prefix, sha256_text(prefix + text)))
        return vec if vec is not None and len(vec) == dim else None

    def put(self, model_sha256: str, prefix: str, text: str, vec: list[float]) -> None:
        self._entries[(model_sha256, prefix, sha256_text(prefix + text))] = list(vec)

    def __len__(self) -> int:
        return len(self._entries)


class CachedEmbedder:
    """包住 PrefixedEmbedder：命中快取的不送模型，未命中的才呼叫並寫回快取。hits／misses 供預算記帳。

    allow_model_calls=False：只用快取；有任何未命中就整批失敗（不呼叫模型），用於「E 已產生快取、應全部命中」的建索引。"""

    def __init__(self, inner: PrefixedEmbedder, cache: EmbeddingCache, *, allow_model_calls: bool = True):
        self._inner, self._cache, self.allow_model_calls = inner, cache, allow_model_calls
        self.provider, self.model, self.dim = inner.provider, inner.model, inner.dim
        self.prefix, self.fingerprint, self.side = inner.prefix, inner.fingerprint, inner.side
        self.model_sha256 = inner.model_sha256
        self.last_input_tokens = None
        self.hits = self.misses = 0

    def input_sha256(self, text: str) -> str:
        return self._inner.input_sha256(text)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        found = [self._cache.get(self.model_sha256, self.prefix, t, self.dim) for t in texts]
        todo = [i for i, v in enumerate(found) if v is None]
        if todo and not self.allow_model_calls:
            raise AdapterCallFailed(f"快取未命中 {len(todo)} 筆（只用快取模式，不呼叫模型）；"
                                    f"第一筆輸入 SHA-256 {sha256_text(self.prefix + texts[todo[0]])}")
        if todo:
            fresh = self._inner.embed([texts[i] for i in todo])
            for i, vec in zip(todo, fresh):
                self._cache.put(self.model_sha256, self.prefix, texts[i], vec)
                found[i] = vec
        self.hits += len(texts) - len(todo)
        self.misses += len(todo)
        return found  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# 生成：B 的 SelectorLLM 介面（contract-v1 evidence_selection.llm_interface）
# ---------------------------------------------------------------------------
class GeneratorAdapter:
    """complete(messages, *, json_schema, max_output_tokens) -> 回覆（text、input_tokens、output_tokens、finish_reason）。

    包住 qa 的生成器（Gemma：GENERATION_PROVIDER=llamacpp，請求帶 response_format json_schema 與
    chat_template_kwargs）；單次呼叫、不重試。應用路徑由 qa.SelectionLLM 加上逾時、有限重試與 stage=selection 記帳；
    離線 runner 可直接用本類別。is_mock 取自生成器（測試替身必須標示）。"""

    def __init__(self, generator, *, timeout_s: float = 60.0, temperature: float = 0.0):
        self.generator, self.timeout_s, self.temperature = generator, timeout_s, temperature
        self.model, self.provider = generator.model, generator.provider
        self.is_mock = bool(getattr(generator, "is_mock", False) or getattr(generator, "mock", False))

    def complete(self, messages: list[dict], *, json_schema: dict | None, max_output_tokens: int):
        return self.generator.generate(messages, timeout_s=self.timeout_s, max_output_tokens=max_output_tokens,
                                       temperature=self.temperature, json_schema=json_schema)
