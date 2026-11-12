"""compact-json-v1（2026-11-06 回答耗時分析）：schema → 不含空白的 GBNF 文法，與生成器開關。

文法本身在本機 llama-server（Gemma 4 E2B）驗證過：接受、輸出 0 個換行、可 json.loads、鍵順序同 schema
（data/review/latency-20261106/grammar_check_e2b.json）。這裡只測轉換規則與開關，不呼叫模型。
"""
from __future__ import annotations

import json

import pytest

from app import answer_policy as AP
from app import json_grammar as G
from app import qa


def test_response_schema_grammar_has_no_whitespace_rule():
    g = G.compact_grammar(AP.RESPONSE_SCHEMA)
    assert g is not None and g.startswith("root ::= ")
    assert "space" not in g and '"\\n"' not in g                       # 沒有任何允許空白的規則
    # 依 properties 順序輸出四個鍵，鍵值之間不留空白
    assert '"{" "\\"insufficient\\":" root-insufficient "," "\\"claims\\":" root-claims' in g
    assert 'root-claims ::= "[" (root-claims-item ("," root-claims-item){0,5})? "]"' in g      # maxItems 6（C-ap-v10 answer-room）、可空
    assert 'root-claims-item-unit-ids ::= "[" root-claims-item-unit-ids-item ("," root-claims-item-unit-ids-item){0,2} "]"' in g  # 1～3
    assert 'root-claims-item-text ::= "\\"" char{0,120} "\\""' in g                                # maxLength 120
    assert "root-clarifying-question ::= root-clarifying-question-string | root-clarifying-question-null" in g
    assert g.rstrip().endswith('char ::= [^"\\\\\\x7F\\x00-\\x1F] | [\\\\] (["\\\\bfnrt] | "u" [0-9a-fA-F]{4})')


@pytest.mark.parametrize("schema", [
    None,
    {"type": "object", "properties": {"a": {"type": "string"}}, "required": [], "additionalProperties": False},   # 可省略屬性
    {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},                              # 沒限制額外屬性
    {"type": "string", "enum": [1, 2]},                                                                         # 非字串列舉
    {"type": "string", "enum": []},
    {"type": "string", "enum": ["x"], "maxLength": 3},
    {"type": "object", "properties": {"n": {"type": "number"}}, "required": ["n"], "additionalProperties": False},
    {"type": "string", "pattern": "^a$"},
])
def test_unsupported_schema_falls_back(schema):
    """不支援的寫法整份放棄（回 None），生成器改用原本的 response_format json_schema。"""
    assert G.compact_grammar(schema) is None


def test_string_enum_v2():
    """v2：實際送出的 schema 把 unit_ids／conditions 限成列舉值（answer_policy.build_answer_prompt）；寫成 JSON 字面值的選擇。"""
    import copy
    schema = copy.deepcopy(AP.RESPONSE_SCHEMA)
    item = schema["properties"]["claims"]["items"]["properties"]
    item["unit_ids"]["items"] = {"type": "string", "enum": ["PR003_h0108#r4", 'a"b', "c\\d", "PR003_h0108#r4"]}
    item["conditions"] = {"type": "array", "maxItems": 2, "items": {"type": "string", "enum": ["乾燥時"]}}
    g = G.compact_grammar(schema)
    assert g is not None and G.COMPACT_JSON_VERSION == "compact-json-v2"
    # 每個值都是含雙引號的 JSON 字串；重複值只留一個；雙引號與反斜線照 JSON 跳脫後再照 GBNF 跳脫
    assert r'root-claims-item-unit-ids-item ::= "\"PR003_h0108#r4\"" | "\"a\\\"b\"" | "\"c\\\\d\""' in g
    assert r'root-claims-item-conditions-item ::= "\"乾燥時\""' in g
    assert 'root-claims-item-conditions ::= "[" (root-claims-item-conditions-item ("," root-claims-item-conditions-item){0,1})? "]"' in g
    item["conditions"] = {"type": "array", "maxItems": 0}           # 沒有原文條件時只能是 []
    assert 'root-claims-item-conditions ::= "[]"' in G.compact_grammar(schema)


def test_array_bounds():
    g = G.compact_grammar({"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "boolean"}})
    assert 'root ::= "[" root-item ("," root-item){1} "]"' in g
    g = G.compact_grammar({"type": "array", "items": {"type": "null"}})
    assert 'root ::= "[" (root-item ("," root-item)*)? "]"' in g


class _Resp:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode()

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _capture(monkeypatch):
    sent = {}

    def fake_urlopen(req, timeout):
        sent["body"] = json.loads(req.data.decode())
        return _Resp({"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                      "usage": {"prompt_tokens": 10, "completion_tokens": 3}})
    monkeypatch.setattr(qa.urllib.request, "urlopen", fake_urlopen)
    return sent


@pytest.mark.parametrize("on", [False, True])
def test_generator_sends_grammar_only_when_on(monkeypatch, on):
    sent = _capture(monkeypatch)
    gen = qa.OpenAICompatibleGenerator("http://127.0.0.1:9", "m", None, provider="llamacpp", compact_json=on)
    gen.generate([{"role": "user", "content": "q"}], timeout_s=5, max_output_tokens=50, temperature=0.0,
                 json_schema=AP.RESPONSE_SCHEMA)
    body = sent["body"]
    if on:
        assert "response_format" not in body and body["grammar"] == G.compact_grammar(AP.RESPONSE_SCHEMA)
        assert gen.last_output_format == G.COMPACT_JSON_VERSION
    else:   # 關閉時請求與原本完全相同
        assert "grammar" not in body and body["response_format"]["type"] == "json_schema"
        assert gen.last_output_format is None


def test_generator_without_schema_keeps_json_object(monkeypatch):
    sent = _capture(monkeypatch)
    gen = qa.OpenAICompatibleGenerator("http://127.0.0.1:9", "m", None, provider="llamacpp", compact_json=True)
    gen.generate([{"role": "user", "content": "q"}], timeout_s=5, max_output_tokens=50, temperature=0.0)
    assert sent["body"]["response_format"] == {"type": "json_object"} and gen.last_output_format is None


def test_config_switch(monkeypatch):
    monkeypatch.setenv("GENERATION_COMPACT_JSON", "on")
    cfg = qa.QaConfig.from_env()
    assert cfg.compact_json is True and cfg.log_view()["compact_json"] == G.COMPACT_JSON_VERSION
    monkeypatch.setenv("GENERATION_COMPACT_JSON", "off")
    assert qa.QaConfig.from_env().log_view()["compact_json"] is None
    monkeypatch.setenv("GENERATION_COMPACT_JSON", "yes")
    with pytest.raises(RuntimeError):
        qa.QaConfig.from_env()
