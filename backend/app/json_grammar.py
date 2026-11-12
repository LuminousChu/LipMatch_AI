"""compact-json-v1（2026-11-06 回答耗時分析）：把輸出 JSON schema 轉成「不允許任何空白」的 llama.cpp GBNF 文法。

為什麼：llama.cpp 把 response_format 的 json_schema 轉成文法時允許空白與換行（space ::= | " " | "\\n"… [ \\t]{0,20}），
Gemma 會輸出排版過的 JSON；40 題重跑的輸出 token 約 40% 是換行縮排，使用者看不到、卻每題多花約 10 秒生成。
本模組產生同一個 schema、但鍵與值之間一律不留空白的文法；後端拿到文字後照舊 json.loads 並用 schema 驗證，
交付內容的檢查不變。

只支援目前輸出契約用到的子集：object（additionalProperties: false、properties 全部 required，依 properties 順序輸出）、
array（items、minItems、maxItems）、string（minLength、maxLength）、字串列舉 enum（v2）、boolean、null、type 為清單的聯集（例如 ["string", "null"]）。
遇到不支援的寫法（number、非字串的 enum、可省略的屬性、pattern…）回 None，呼叫端退回原本的 response_format json_schema。

v2（2026-11-06）：實際送給模型的 schema 會把 unit_ids、conditions 限成本次證據的列舉值（answer_policy 依送入的單位產生），
v1 不支援 enum，12B 40 題全部退回原路徑、沒有真的用到單行文法；v2 把字串列舉轉成 JSON 字串字面值的選擇（與 llama.cpp 相同）。
字元規則與 llama.cpp 的 json_schema_to_grammar 相同。
"""
from __future__ import annotations

import json

COMPACT_JSON_VERSION = "compact-json-v2"
# 與 llama.cpp json_schema_to_grammar 的 char 規則相同：一般字元，或合法的跳脫序列
_CHAR_RULE = r'[^"\\\x7F\x00-\x1F] | [\\] (["\\bfnrt] | "u" [0-9a-fA-F]{4})'
_IGNORED_KEYS = {"description", "title", "$schema", "examples", "default"}


class _Unsupported(Exception):
    pass


def _literal(text: str) -> str:
    """GBNF 字串字面值（雙引號包起來，裡面的反斜線與雙引號跳脫）。"""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _repeat(rule: str, lo: int, hi: int | None) -> str:
    if hi is None:
        return f"{rule}*" if lo == 0 else f"{rule}{{{lo},}}"
    if lo == hi:
        return f"{rule}{{{lo}}}"
    return f"{rule}{{{lo},{hi}}}"


class _Builder:
    def __init__(self) -> None:
        self.rules: dict[str, str] = {}
        self.uses_char = False

    def add(self, name: str, body: str) -> str:
        self.rules[name] = body
        return name

    def visit(self, schema: dict, name: str) -> str:
        """回傳代表這個 schema 的規則名稱。"""
        if not isinstance(schema, dict):
            raise _Unsupported("schema 不是物件")
        unknown = set(schema) - _IGNORED_KEYS - {"type", "properties", "required", "additionalProperties",
                                                 "items", "minItems", "maxItems", "minLength", "maxLength", "enum"}
        if unknown:
            raise _Unsupported(f"不支援的關鍵字：{sorted(unknown)}")
        types = schema.get("type")
        if "enum" in schema:
            return self._enum(schema, name)
        if isinstance(types, list):
            if not types or len(set(types)) != len(types):
                raise _Unsupported("type 清單不合法")
            alts = [self.visit({**schema, "type": t}, f"{name}-{t}") for t in types]
            return self.add(name, " | ".join(alts))
        if types == "object":
            return self._object(schema, name)
        if types == "array":
            return self._array(schema, name)
        if types == "string":
            self.uses_char = True
            lo, hi = int(schema.get("minLength", 0)), schema.get("maxLength")
            return self.add(name, f'"\\"" {_repeat("char", lo, None if hi is None else int(hi))} "\\""')
        if types == "boolean":
            return self.add(name, '"true" | "false"')
        if types == "null":
            return self.add(name, '"null"')
        raise _Unsupported(f"不支援的型別：{types!r}")

    def _enum(self, schema: dict, name: str) -> str:
        """字串列舉：每個值寫成 JSON 字串字面值（json.dumps），彼此是選擇；只接受 type 為 string 或沒寫 type。"""
        values = schema["enum"]
        if schema.get("type", "string") != "string" or set(schema) - _IGNORED_KEYS - {"type", "enum"}:
            raise _Unsupported("enum 只支援單純的字串列舉")
        if not isinstance(values, list) or not values or not all(isinstance(v, str) for v in values):
            raise _Unsupported("enum 要是非空的字串清單")
        return self.add(name, " | ".join(_literal(json.dumps(v, ensure_ascii=False)) for v in dict.fromkeys(values)))

    def _object(self, schema: dict, name: str) -> str:
        props = schema.get("properties") or {}
        if schema.get("additionalProperties", True) is not False:
            raise _Unsupported("物件要 additionalProperties: false")
        if set(schema.get("required") or []) != set(props):
            raise _Unsupported("只支援全部屬性都 required 的物件")
        if not props:
            return self.add(name, '"{}"')
        parts = []
        for i, (key, sub) in enumerate(props.items()):
            value = self.visit(sub, f"{name}-{_slug(key)}")
            parts.append(("\",\" " if i else "") + f'{_literal(json.dumps(key, ensure_ascii=False) + ":")} {value}')
        return self.add(name, '"{" ' + " ".join(parts) + ' "}"')

    def _array(self, schema: dict, name: str) -> str:
        lo = int(schema.get("minItems", 0))
        hi = schema.get("maxItems")
        hi = None if hi is None else int(hi)
        if hi is not None and hi < lo:
            raise _Unsupported("maxItems 小於 minItems")
        if hi == 0:                      # 只能是空陣列（例如沒有原文條件時的 conditions），不需要 items
            return self.add(name, '"[]"')
        item = self.visit(schema.get("items") or {}, f"{name}-item")
        rest = _repeat(f'("," {item})', max(lo - 1, 0), None if hi is None else hi - 1)
        body = f"{item} {rest}"
        return self.add(name, f'"[" {body} "]"' if lo >= 1 else f'"[" ({body})? "]"')


def _slug(key: str) -> str:
    out = "".join(ch if ch.isascii() and ch.isalnum() else "-" for ch in key).strip("-")
    return out or "k"


def compact_grammar(schema: dict | None) -> str | None:
    """schema → 不含空白的 GBNF 文法字串；不支援的 schema 回 None（呼叫端退回 response_format json_schema）。"""
    if not schema:
        return None
    b = _Builder()
    try:
        top = b.visit(schema, "root")
    except _Unsupported:
        return None
    lines = [f"root ::= {b.rules.pop(top)}"] if top == "root" else [f"root ::= {top}"]
    lines += [f"{n} ::= {body}" for n, body in b.rules.items()]
    if b.uses_char:
        lines.append(f"char ::= {_CHAR_RULE}")
    return "\n".join(lines) + "\n"
# 核心邏輯：
# 1. 物件依 properties 順序輸出每個鍵（鍵含冒號直接寫成字面值），鍵值之間、逗號前後都不留空白。
# 2. 陣列依 minItems／maxItems 展開成「第一個項目＋（逗號＋項目）重複」；minItems 為 0 時整組可省略，輸出 []。
# 3. 字串用和 llama.cpp 相同的 char 規則，maxLength 轉成重複次數上限（和 llama.cpp 原本的轉換一樣限制長度）。
#    字串列舉（v2）寫成各個值的 JSON 字面值之間的選擇，模型只能照抄其中一個。
# 4. 任何不支援的寫法都整份放棄，交回原本的 json_schema 路徑，不會產生比原本更寬鬆的文法。
