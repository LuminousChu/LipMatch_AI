"""S7 推薦情境執行器：用 SYNTHETIC 合成候選跑驗收情境，輸出「推薦取捨紀錄」（Markdown 或 JSON）。

    python -m app.rec_cases               # Markdown（寫進 docs/mvp/s7-decision-log.md 用）
    python -m app.rec_cases --json        # 機器可讀

流程與 API 相同：使用者原話 → S6 規則解析（rule-v1）→ 偏好狀態 → S7 decide()。
差別只在候選來源：這裡由合成候選檔直接建立（證據有效與否由情境指定，模擬資料庫的判斷結果）；
資料庫中「來源停用 → 證據失效」的真實路徑另由 tests/test_recommend.py 驗證。
SYNTHETIC：結果只證明規則照設計運作，不是正式推薦、不代表任何色系適合任何人。
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

from . import preferences as pref
from . import recommend as rec

SYNTHETIC_FILE = Path(__file__).resolve().parent.parent / "eval" / "s7_synthetic_candidates.json"
_NOW = "2026-10-15T00:00:00+00:00"

# 驗收情境：同一個情境內的訊息依序送進同一個對話。invalid 指定哪些證據失效（模擬資料庫判斷）。
SCENARIOS: list[dict] = [
    {"id": "S7-01", "group": "硬排除", "title": "上班、自然、不要霧面",
     "messages": ["上班用的自然唇色，不要霧面"],
     "expect": {"status": "recommended", "primary": "synthetic-nude-satin",
                "alternatives": ["synthetic-rose-satin"],
                "excluded": {"synthetic-bean-paste-matte": "H02", "synthetic-coral-unknown": "H04"}}},
    {"id": "S7-02", "group": "偏好變更", "title": "接續 S7-01，改成聚會（不要霧面須保留）",
     "messages": ["上班用的自然唇色，不要霧面", "改成聚會"],
     "expect": {"status": "recommended", "primary": "synthetic-rose-satin",
                "alternatives": ["synthetic-nude-satin", "synthetic-red-gloss"],
                "excluded": {"synthetic-bean-paste-matte": "H02", "synthetic-coral-unknown": "H04"}}},
    {"id": "S7-03a", "group": "無候選", "title": "所有候選都違反硬條件 → no_match，只問是否放寬",
     "messages": ["推薦唇色，不要霧面，不要裸色，不要玫瑰色，不要紅色"],
     "expect": {"status": "no_match", "primary": None, "alternatives": []}},
    {"id": "S7-03b", "group": "無候選", "title": "沒有任何經核對的候選（目前正式資料庫的實際狀態）",
     "messages": ["上班用的自然唇色，不要霧面"], "no_candidates": True,
     "expect": {"status": "insufficient_evidence", "primary": None, "alternatives": []}},
    {"id": "S7-04a", "group": "規則來源停用", "title": "S7-01 條件，但裸色的色系證據所在文件被停用",
     "messages": ["上班用的自然唇色，不要霧面"],
     "invalid": {"SYN_S7_B_NUDE_SATIN_color_family_nude": "document_inactive"},
     "expect": {"status": "recommended", "primary": "synthetic-rose-satin", "alternatives": [],
                "excluded": {"synthetic-nude-satin": "G02"}}},
    {"id": "S7-04b", "group": "規則來源停用", "title": "S7-01 條件，但裸色「職場」的證據片段被退回審核",
     "messages": ["上班用的自然唇色，不要霧面"],
     "invalid": {"SYN_S7_A_NUDE_SATIN_occasions_office": "chunk_not_approved"},
     "expect": {"status": "recommended", "primary": "synthetic-nude-satin",
                "alternatives": ["synthetic-rose-satin"], "tied_primary": ["synthetic-rose-satin"]}},
    # 2026-10-18 K1 修訂：舊預期主推 coral、替代 nude／rose（三者有日常證據，靠「預設日常」打破平手）。
    # 場合未指定不再假設日常 → 五個候選都沒有可比較的條件、完全同分，依 option_key 穩定排序並揭露「同樣符合」。
    {"id": "S7-05", "group": "揭露", "title": "自述小麥色、場合未指定 → 不假設日常、不宣稱膚色適配",
     "messages": ["我是小麥色，推薦唇色"],
     "expect": {"status": "recommended", "primary": "synthetic-bean-paste-matte",
                "alternatives": ["synthetic-coral-unknown", "synthetic-nude-satin"]}},
    {"id": "S7-06", "group": "揭露", "title": "一般知識問題不強制推薦",
     "messages": ["豆沙色是什麼意思"],
     "expect": {"status": "not_applicable", "primary": None, "alternatives": []}},
]


# 由合成候選檔建立 Candidate；chunk_id 依出現順序編號，invalid 指定的證據標成失效。
def candidates_from_spec(spec: dict, invalid: dict[str, str] | None = None) -> list[rec.Candidate]:
    invalid = invalid or {}
    out, next_id = [], 1
    for i, c in enumerate(spec["candidates"], start=1):
        evidence = []
        for e in c["evidence"]:
            reason = invalid.get(e["chunk_key"])
            evidence.append(rec.Evidence(
                chunk_id=next_id, chunk_key=e["chunk_key"], doc_key=e["doc_key"], version_label=e["version_label"],
                title=f"SYNTHETIC {e['doc_key']}", page_start=1, page_end=1, html_locator=None,
                support_type=e["support_type"], claim=e["claim"], supports=list(e["supports"]),
                valid=reason is None, invalid_reason=reason))
            next_id += 1
        out.append(rec.Candidate(option_id=i, option_key=c["option_key"], name=c["name"],
                                 description=c["description"], attributes=copy.deepcopy(c["attributes"]),
                                 limitations=list(c.get("limitations", [])), evidence=evidence,
                                 synthetic=bool(spec.get("synthetic"))))
    return out


# 執行一個情境：依序處理每則訊息（與 API 相同的 S6→S7 流程），回傳最後一則的結果與每一輪紀錄。
def run_scenario(sc: dict, spec: dict) -> dict:
    parser = pref.RuleParser()
    state = pref.empty_state()
    cands = [] if sc.get("no_candidates") else candidates_from_spec(spec, sc.get("invalid"))
    had_prior, turns, decision = False, [], None
    for msg in sc["messages"]:
        parsed = parser.parse(msg)
        turn = pref.process_parsed(state, parsed, request_id=None, now=_NOW)
        state = turn.state
        decision = rec.decide(state, cands, intent=parsed.intent, secondary=parsed.secondary_intents,
                              had_prior=had_prior)
        if decision.status not in ("not_applicable",):
            had_prior = True
        turns.append({"message": msg, "intent": parsed.intent, "status": decision.status,
                      "primary": _primary(decision), "alternatives": _alts(decision)})
    return {"scenario": sc, "turns": turns, "decision": decision, "state": state,
            "check": _check(sc["expect"], decision)}
# 核心邏輯：had_prior 模擬「這個對話之前已在推薦流程中」，所以「改成聚會」這種純修改訊息會重新推薦。


# 主推的 option_key（沒有推薦時為 None）。
def _primary(d: rec.Decision) -> str | None:
    return d.recommendations[0]["option_key"] if d.recommendations else None


# 替代的 option_key 清單。
def _alts(d: rec.Decision) -> list[str]:
    return [r["option_key"] for r in d.recommendations[1:]]


# 比對預期；回傳不符合的項目（空清單＝通過）。
def _check(expect: dict, d: rec.Decision) -> list[str]:
    problems = []
    if d.status != expect["status"]:
        problems.append(f"status {d.status} ≠ {expect['status']}")
    if _primary(d) != expect["primary"]:
        problems.append(f"主推 {_primary(d)} ≠ {expect['primary']}")
    if _alts(d) != expect["alternatives"]:
        problems.append(f"替代 {_alts(d)} ≠ {expect['alternatives']}")
    for key, rule in (expect.get("excluded") or {}).items():
        t = next((x for x in d.trace if x["option_key"] == key), None)
        if t is None or t["decision"] != "excluded" or rule not in {r["rule"] for r in t["reasons"]}:
            problems.append(f"{key} 應被 {rule} 排除")
    if "tied_primary" in expect and d.recommendations and d.recommendations[0]["tied_with"] != expect["tied_primary"]:
        problems.append(f"同分 {d.recommendations[0]['tied_with']} ≠ {expect['tied_primary']}")
    return problems


# ---------------------------------------------------------------------------
# 輸出
# ---------------------------------------------------------------------------
_DECISION_LABELS = {"selected_primary": "主推", "selected_alternative": "替代", "excluded": "排除",
                    "not_selected": "未選", "eligible": "通過未選"}


# 把一個情境的結果寫成 Markdown 段落：每輪狀態、每個候選的取捨與原因、回覆模板。
def _markdown_one(r: dict) -> list[str]:
    sc, d = r["scenario"], r["decision"]
    lines = [f"### {sc['id']}｜{sc['group']}：{sc['title']}", ""]
    if sc.get("invalid"):
        lines.append("失效的證據（模擬資料庫判斷）：" + "、".join(f"`{k}`（{v}）" for k, v in sc["invalid"].items()))
        lines.append("")
    for t in r["turns"]:
        lines.append(f"- 訊息「{t['message']}」→ 意圖 `{t['intent']}`，推薦狀態 `{t['status']}`，"
                     f"主推 `{t['primary']}`，替代 `{t['alternatives']}`")
    hard = "、".join(f"不要{h['label']}" for h in d.hard_conditions) or "無"
    soft = "、".join(f"{pref.FIELD_LABELS.get(s['field'], s['field'])}={'/'.join(s['labels'])}"
                     for s in d.soft_preferences) or "無"
    lines += ["", f"硬條件：{hard}；軟偏好：{soft}；預設：{'、'.join(a['text'] for a in d.assumptions) or '無'}", ""]
    if d.trace:
        lines += ["| 候選 | 取捨 | 原因／排序鍵 |", "|---|---|---|"]
        for t in d.trace:
            if t["decision"] == "excluded":
                why = "；".join(f"{x['rule']} {x['code']}" + (f"（{x.get('label')}）" if x.get("label") else "")
                               for x in t["reasons"])
            else:
                why = f"符合 {list(t.get('matched', {}))}；排序鍵 {t.get('sort_key')}"
                if t.get("not_selected_reason"):
                    why += f"；未選：{t['not_selected_reason']}"
            lines.append(f"| `{t['option_key']}` | {_DECISION_LABELS.get(t['decision'], t['decision'])} | {why} |")
        lines.append("")
    text = rec.render_text(d)
    if text:
        lines += ["回覆模板：", "", "```text", text, "```", ""]
    if d.question:
        lines += [f"補問：{d.question}", ""]
    if d.limitations:
        lines += ["限制：" + " ".join(d.limitations), ""]
    lines.append("驗收：" + ("**通過**" if not r["check"] else "**不符**：" + "；".join(r["check"])))
    lines.append("")
    return lines


# 命令列入口：跑全部情境，輸出 Markdown 或 JSON；有任何情境不符預期就以非 0 結束。
def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m app.rec_cases")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    spec = json.loads(SYNTHETIC_FILE.read_text(encoding="utf-8"))
    results = [run_scenario(sc, spec) for sc in SCENARIOS]
    passed = sum(1 for r in results if not r["check"])
    if args.json:
        print(json.dumps([{"id": r["scenario"]["id"], "turns": r["turns"], "check": r["check"],
                           "decision": r["decision"].to_dict()} for r in results], ensure_ascii=False, indent=2))
    else:
        out = [f"規則版本 `{rec.RULES_VERSION}`；偏好解析 `{pref.PARSER_VERSION}`；候選：SYNTHETIC "
               f"`{SYNTHETIC_FILE.name}`。結果 **{passed}/{len(results)} 通過**。", ""]
        for r in results:
            out += _markdown_one(r)
        print("\n".join(out))
    if passed != len(results):
        sys.exit(1)


if __name__ == "__main__":
    main()
