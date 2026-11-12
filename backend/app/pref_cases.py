"""S6 成對修改案例執行器：依序把每個案例的語句送進同一個新對話，檢查最後的偏好狀態。

用法（在 backend/ 目錄）：
    python -m app.pref_cases              # 印出 Markdown 結果表
    python -m app.pref_cases --json       # 印出 JSON（含每一步的狀態摘要）

案例檔：backend/eval/s6_pair_cases.json（SYNTHETIC 開發案例，非正式驗收）。
這裡不連資料庫、不呼叫模型；用的是與 API 完全相同的 preferences.process_parsed()。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import preferences as pref

CASES_PATH = Path(__file__).resolve().parent.parent / "eval" / "s6_pair_cases.json"


# 讀取案例檔。
def load_cases(path: Path = CASES_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# 在一個全新的對話狀態上依序執行案例的每一步，回傳最後一輪的結果。
def run_steps(steps: list[str], parser: pref.RuleParser | None = None) -> pref.TurnResult:
    parser = parser or pref.RuleParser()
    state = pref.empty_state()
    turn: pref.TurnResult | None = None
    for i, text in enumerate(steps, start=1):
        now = datetime(2026, 10, 15, 12, 0, i, tzinfo=timezone.utc).isoformat()
        turn = pref.process_parsed(state, parser.parse(text), request_id=f"step-{i}", now=now)
        state = turn.state
    assert turn is not None, "案例至少要有一步"
    return turn
# 核心邏輯：每個案例都從 empty_state() 開始（等同新對話），時間固定，結果可重現。


# 從最後一輪結果取出要比對的實際值（鍵名與案例檔 expect 相同）。
def observe(turn: pref.TurnResult) -> dict:
    s = turn.state
    obs = {f: [i["value"] for i in s[f]] for f in pref.LIST_FIELDS}
    obs.update({
        "occasion": s["occasion"]["value"], "occasion_status": s["occasion"]["status"],
        "occasion_values": list(s["occasion"].get("values") or []),          # rule-v5 多情境（K3）
        "undertone_status": s["undertone"]["status"],                         # rule-v5 暫定底調（K4）
        "undertone_tentative": s["undertone"].get("tentative"),
        "skin_tone": s["skin_tone"]["value"], "undertone": s["undertone"]["value"],
        "natural_lip": s["natural_lip"]["value"],
        "budget_max": (s["budget"]["value"] or {}).get("max"),
        "allergy_status": s["allergy"]["status"], "allergy_avoid": [i["value"] for i in s["allergy"]["avoid"]],
        "conflicts": len(s["conflicts"]), "ambiguities": len(s["ambiguities"]),
        "blocking_questions": sum(1 for q in turn.clarification if q["blocking"]),
        "max_questions": len(turn.clarification),
        "last_intent": turn.parsed.intent,
        "limitations": turn.limitations,
        "questions": [q["question"] for q in turn.clarification],
    })
    return obs


# 比對一個案例：回傳 (是否全部通過, 不符項目清單, 實際值)。
def check_case(case: dict) -> tuple[bool, list[str], dict]:
    obs = observe(run_steps(case["steps"]))
    failures: list[str] = []
    for key, want in case["expect"].items():
        if key == "limitation_contains":
            if not any(want in x for x in obs["limitations"]):
                failures.append(f"limitations 不含「{want}」：{obs['limitations']}")
        elif key == "max_questions":
            if obs["max_questions"] > want:
                failures.append(f"補問 {obs['max_questions']} 題 > 上限 {want}")
        elif isinstance(want, list):
            if sorted(obs[key]) != sorted(want):
                failures.append(f"{key}：預期 {want}，實際 {obs[key]}")
        elif obs[key] != want:
            failures.append(f"{key}：預期 {want!r}，實際 {obs[key]!r}")
    return not failures, failures, obs
# 核心邏輯：清單欄位比對集合（順序不重要）；max_questions 是上限；limitation_contains 是子字串比對。


# 執行全部案例並輸出 Markdown 或 JSON；有任何案例失敗時回傳非 0。
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="S6 成對修改案例")
    ap.add_argument("--json", action="store_true", help="輸出 JSON")
    args = ap.parse_args(argv)
    data = load_cases()
    results = []
    for case in data["cases"]:
        ok, failures, obs = check_case(case)
        results.append({"id": case["id"], "spec": case["spec"], "title": case["title"], "steps": case["steps"],
                        "passed": ok, "failures": failures, "observed": obs})
    passed = sum(r["passed"] for r in results)
    if args.json:
        print(json.dumps({"case_version": data["case_version"], "parser": pref.PARSER_VERSION,
                          "vocab": pref.VOCAB["version"], "passed": passed, "total": len(results),
                          "results": results}, ensure_ascii=False, indent=2))
    else:
        print(f"案例版本 {data['case_version']}｜解析器 {pref.PARSER_VERSION}｜詞彙 {pref.VOCAB['version']}"
              f"｜通過 {passed}/{len(results)}\n")
        print("| ID | 規格 | 案例 | 語句（依序） | 結果 | 補問 |")
        print("|---|---|---|---|---|---|")
        for r in results:
            steps = " → ".join(f"「{s}」" for s in r["steps"])
            verdict = "通過" if r["passed"] else "失敗：" + "；".join(r["failures"])
            questions = "／".join(r["observed"]["questions"]) or "—"
            print(f"| {r['id']} | {r['spec']} | {r['title']} | {steps} | {verdict} | {questions} |")
    return 0 if passed == len(results) else 1
# 核心邏輯：結果表可直接貼進交付文件；補問欄顯示實際問句，方便人工檢查問法是否恰當。


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
