"""修復 D：本機模型接口（xVITA 嵌入／Gemma 生成）的設定、請求格式、錯誤分類與快取重用規則。

MOCK：本檔的 HTTP 伺服器是本機假 llama-server，只回固定格式的假向量／假回答，
用來驗證「送出什麼、怎麼解析、何時報錯」；不是真模型，不能當成相容性或品質驗收（真模型由 E 排程）。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app import model_adapters as ma
from app import qa, retrieval

MODEL_SHA = "a" * 64
DOC_PREFIX = "title: none | text: "
QUERY_PREFIX = "task: search result | query: "


class _FakeLlama(BaseHTTPRequestHandler):
    """MOCK llama-server：/embedding 回每段輸入長度構成的 768 維假向量；/v1/chat/completions 回固定 JSON。"""

    mode = "ok"
    seen: list = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8"))
        type(self).seen.append({"path": self.path, "body": body, "auth": self.headers.get("Authorization")})
        mode = type(self).mode
        if mode == "500":
            self.send_response(500)
            self.end_headers()
            return
        if self.path == "/embedding":
            dim = 512 if mode == "wrong_dim" else 768
            rows = []
            for i, text in enumerate(body["content"]):
                vec = [float(len(text) % 7 + 1)] * dim
                emb = [vec, vec] if mode == "per_token" else ([vec] if mode == "nested" else vec)
                rows.append({"index": i, "embedding": emb})
            payload = list(reversed(rows))  # 故意倒序：客戶端必須依 index 排回
        else:
            payload = {"choices": [{"message": {"content": "{\"ok\": true}"}, "finish_reason": "stop"}],
                       "usage": {"prompt_tokens": 11, "completion_tokens": 3}}
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def llama():
    _FakeLlama.mode, _FakeLlama.seen = "ok", []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeLlama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _env(base_url, **over):
    env = {"EMBEDDING_PROVIDER": "llamacpp", "EMBEDDING_BASE_URL": base_url,
           "EMBEDDING_MODEL": "MOCK-xvita", "EMBEDDING_DIM": "768", "EMBEDDING_MODEL_SHA256": MODEL_SHA,
           "EMBEDDING_DOC_PREFIX": DOC_PREFIX, "EMBEDDING_QUERY_PREFIX": QUERY_PREFIX, "EMBEDDING_BATCH": "2"}
    env.update(over)
    return {k: v for k, v in env.items() if v is not None}


# ---------------------------------------------------------------------------
# 設定：未設定／不完整一律明確報錯，不產生假向量
# ---------------------------------------------------------------------------
def test_not_configured_is_explicit():
    with pytest.raises(ma.AdapterNotConfigured, match="EMBEDDING_PROVIDER=未設定"):
        ma.EmbeddingSettings.from_env({})
    with pytest.raises(ma.AdapterNotConfigured, match="EMBEDDING_MODEL_SHA256.*EMBEDDING_QUERY_PREFIX"):
        ma.EmbeddingSettings.from_env(_env("http://x", EMBEDDING_MODEL_SHA256=None, EMBEDDING_QUERY_PREFIX=None))
    with pytest.raises(ma.AdapterNotConfigured, match="64 位"):
        ma.EmbeddingSettings.from_env(_env("http://x", EMBEDDING_MODEL_SHA256="abc"))
    # retrieval 的入口沿用 EmbeddingUnavailable（qa 會回 retrieval_not_configured）
    with pytest.raises(retrieval.EmbeddingUnavailable, match="DEC-11"):
        retrieval.get_embedder({})


def test_empty_prefix_must_be_explicit_but_allowed():
    s = ma.EmbeddingSettings.from_env(_env("http://x", EMBEDDING_DOC_PREFIX="", EMBEDDING_QUERY_PREFIX=""))
    assert (s.doc_prefix, s.query_prefix) == ("", "")
    assert s.fingerprint() == {"model": "MOCK-xvita", "model_sha256": MODEL_SHA, "dim": 768,
                               "doc_prefix": "", "query_prefix": ""}


# ---------------------------------------------------------------------------
# 嵌入：前綴、批次、順序、維度與錯誤
# ---------------------------------------------------------------------------
def test_document_and_query_sides_use_their_own_prefix(llama):
    docs = retrieval.get_document_embedder(_env(llama))
    queries = retrieval.get_embedder(_env(llama))
    assert (docs.side, queries.side) == ("document", "query")
    assert docs.fingerprint == queries.fingerprint and docs.dim == 768
    vecs = docs.embed(["一", "二二", "三三三"])          # batch=2 → 兩次請求
    assert [len(v) for v in vecs] == [768] * 3
    assert [v[0] for v in vecs] == [float(len(DOC_PREFIX + t) % 7 + 1) for t in ["一", "二二", "三三三"]]
    queries.embed(["問"])
    sent = [s["body"]["content"] for s in _FakeLlama.seen]
    assert sent == [[DOC_PREFIX + "一", DOC_PREFIX + "二二"], [DOC_PREFIX + "三三三"], [QUERY_PREFIX + "問"]]
    assert docs.input_sha256("一") == ma.sha256_text(DOC_PREFIX + "一")
    calls = docs.calls
    assert len(calls) == 2 and all(c["input_tokens"] is None and c["error"] is None for c in calls)  # 文件端兩批


def test_nested_single_row_accepted(llama):
    _FakeLlama.mode = "nested"
    assert len(retrieval.get_embedder(_env(llama)).embed(["x"])[0]) == 768


@pytest.mark.parametrize("mode,match", [("wrong_dim", "維度 512"), ("per_token", "逐 token"),
                                        ("500", "HTTP 500")])
def test_embedding_errors_are_raised_not_hidden(llama, mode, match):
    _FakeLlama.mode = mode
    emb = retrieval.get_embedder(_env(llama))
    with pytest.raises(ma.AdapterCallFailed, match=match):
        emb.embed(["x"])
    assert emb.calls[-1]["error"] is not None


def test_unreachable_server():
    emb = retrieval.get_embedder(_env("http://127.0.0.1:9", EMBEDDING_TIMEOUT_S="1"))
    with pytest.raises(ma.AdapterCallFailed, match="無法連線|未回應"):
        emb.embed(["x"])


# ---------------------------------------------------------------------------
# 快取：模型檔 SHA-256＋前綴＋完整輸入 SHA-256 三者相同才重用
# ---------------------------------------------------------------------------
def test_cache_reuse_requires_model_prefix_and_text_match(llama, tmp_path):
    cache = ma.EmbeddingCache()
    docs = ma.CachedEmbedder(retrieval.get_document_embedder(_env(llama)), cache)
    first = docs.embed(["SYNTHETIC 段落"])
    assert (docs.hits, docs.misses, len(_FakeLlama.seen)) == (0, 1, 1)
    assert docs.embed(["SYNTHETIC 段落"]) == first and (docs.hits, docs.misses) == (1, 1)
    assert len(_FakeLlama.seen) == 1                       # 命中不再呼叫模型

    other_prefix = ma.CachedEmbedder(retrieval.get_embedder(_env(llama)), cache)   # 查詢端前綴不同
    other_prefix.embed(["SYNTHETIC 段落"])
    other_model = ma.CachedEmbedder(retrieval.get_document_embedder(_env(llama, EMBEDDING_MODEL_SHA256="b" * 64)),
                                    cache)
    other_model.embed(["SYNTHETIC 段落"])
    near_text = ma.CachedEmbedder(retrieval.get_document_embedder(_env(llama)), cache)
    near_text.embed(["SYNTHETIC 段落 "])                    # 多一個空白也不算同一段
    assert (other_prefix.misses, other_model.misses, near_text.misses) == (1, 1, 1)

    path = tmp_path / "cache.jsonl"
    cache.save(path)
    again = ma.EmbeddingCache.load(path)
    assert len(again) == len(cache) == 4
    assert again.get(MODEL_SHA, DOC_PREFIX, "SYNTHETIC 段落", 768) == first[0]
    assert again.get(MODEL_SHA, DOC_PREFIX, "SYNTHETIC 段落", 1024) is None   # 維度不同也不重用
    assert "SYNTHETIC 段落" not in path.read_text(encoding="utf-8")            # 快取不存原文


# ---------------------------------------------------------------------------
# 生成：llamacpp provider（Gemma）請求格式
# ---------------------------------------------------------------------------
def test_llamacpp_generator_request_shape(llama):
    cfg = qa.QaConfig(provider="llamacpp", model="MOCK-gemma", base_url=f"{llama}/v1",
                      chat_template_kwargs={"enable_thinking": False}, cache_prompt=False)
    gen = qa.get_generator(cfg)
    assert gen.provider == "llamacpp"
    res = gen.generate([{"role": "user", "content": "hi"}], timeout_s=5, max_output_tokens=50, temperature=0.0)
    assert (res.input_tokens, res.output_tokens) == (11, 3)
    seen = _FakeLlama.seen[-1]
    assert seen["path"] == "/v1/chat/completions" and seen["auth"] is None
    body = seen["body"]
    assert body["chat_template_kwargs"] == {"enable_thinking": False} and body["cache_prompt"] is False
    assert body["max_tokens"] == 50 and "tools" not in body and "functions" not in body


def test_llamacpp_generator_omits_unset_options(llama):
    gen = qa.get_generator(qa.QaConfig(provider="llamacpp", model="MOCK-gemma", base_url=f"{llama}/v1"))
    gen.generate([{"role": "user", "content": "hi"}], timeout_s=5, max_output_tokens=5, temperature=0.0)
    body = _FakeLlama.seen[-1]["body"]
    assert "chat_template_kwargs" not in body and "cache_prompt" not in body


def test_llamacpp_requires_model_and_url():
    assert isinstance(qa.get_generator(qa.QaConfig(provider="llamacpp")), qa.NotConfiguredGenerator)


def test_qa_config_from_env(monkeypatch):
    monkeypatch.setenv("GENERATION_PROVIDER", "llamacpp")
    monkeypatch.setenv("GENERATION_CHAT_TEMPLATE_KWARGS", '{"enable_thinking": false}')
    monkeypatch.setenv("GENERATION_CACHE_PROMPT", "true")
    monkeypatch.setenv("QA_PIPELINE", "repair")
    monkeypatch.setenv("QA_K_POOL", "30")
    cfg = qa.QaConfig.from_env()
    assert (cfg.provider, cfg.chat_template_kwargs, cfg.cache_prompt, cfg.pipeline, cfg.k_pool) == (
        "llamacpp", {"enable_thinking": False}, True, "repair", 30)
    monkeypatch.setenv("GENERATION_CACHE_PROMPT", "maybe")
    with pytest.raises(RuntimeError, match="GENERATION_CACHE_PROMPT"):
        qa.QaConfig.from_env()


def test_cache_only_mode_never_calls_model(llama):
    cache = ma.EmbeddingCache()
    docs = retrieval.get_document_embedder(_env(llama))
    cache.put(MODEL_SHA, DOC_PREFIX, "已快取", [0.5] * 768)
    only = ma.CachedEmbedder(docs, cache, allow_model_calls=False)
    assert only.embed(["已快取"]) == [[0.5] * 768] and _FakeLlama.seen == []
    with pytest.raises(ma.AdapterCallFailed, match="快取未命中 1 筆"):
        only.embed(["已快取", "沒快取"])
    assert _FakeLlama.seen == []                               # 未命中也不呼叫模型
