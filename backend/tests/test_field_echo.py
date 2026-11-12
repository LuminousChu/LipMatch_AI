"""field-echo-v1（2026-11-07）：模型把欄位名稱當成限制寫出時拿掉（exp4 P08 實際輸出）。"""
import json

from app import postprocess as P


def _raw(lims):
    return json.dumps({"insufficient": True, "claims": [], "limitations": lims, "clarifying_question": None},
                      ensure_ascii=False)


def test_field_name_limitation_dropped():
    a = P.parse_answer(_raw(["證據未提供藍調紅與奶茶色在面試專業感上的具體對比。", "clarifying_question"]))
    assert a.limitations == ["證據未提供藍調紅與奶茶色在面試專業感上的具體對比。"]
    assert a.errors == []


def test_every_field_name_dropped_but_sentences_kept():
    names = sorted(P.OUTPUT_FIELD_NAMES)
    a = P.parse_answer(_raw(names + [" claims ", "證據沒有 claims 的說明。"]))
    assert a.limitations == ["證據沒有 claims 的說明。"]     # 前後空白會先去掉；句子裡含欄位名稱不動
