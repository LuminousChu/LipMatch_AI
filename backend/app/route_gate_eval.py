"""路由守門實驗的評估執行器：比較「現行規則路由」與「規則攔截＋softmax 守門」哪個比較常走對路。

用法（在 backend/ 目錄；守門模型要先開好，例如本機 llama-server 8631 跑 gemma-4-E2B）：
    python -X utf8 -m app.route_gate_eval \
        --labels ../docs/mvp/route-gate-20261031/route_labels.json \
        --questions ../data/review/t4-40q-20261031/questions_gold.json \
                    ../data/review/t4-31q-20261031/questions_gold.json \
        --records ../data/review/t4-40q-20261031/t4_40q \
                  ../data/review/t4-31q-20261031/t4_31q \
        --base-url http://127.0.0.1:8631/v1 --model gemma-4-E2B-it-Q4_K_M \
        --out ../data/review/route-softmax-gate-20261031/e2b
    （遠端 T4：--base-url 換成通道網址＋/v1，加 --api-key-env 指定放金鑰的環境變數名稱；金鑰不寫進輸出）
    重新計分（不呼叫模型）：python -X utf8 -m app.route_gate_eval --rescore <結果.json> [...]

比較的三個系統（第三個是對照組，用來拆開規則改動與模型守門各自的功勞）
    現行（route-v2）：直接讀 T4 跑測紀錄（*_record.json）裡實際走的路，不重跑。
    新（規則攔截＋守門）：
        1. 規則攔截：preferences.RuleParser 判成 outside_scope 或 product_request → 攔截（商品不再出卡）
        2. 其他交給 route_gate.classify()：
             card    → 規則推薦卡真的能涵蓋（紀錄裡現行是 card_covers_request）才出卡，否則退到問答
             qa      → 問答
             clarify → 先澄清
             outside → 攔截
    對照（只改規則攔截）：第 1 步相同，其他維持現行實際走的路，不用模型。
評分
    每題的標準答案是「可接受的路徑集合」（route_labels.json），走的路在集合裡就算對。
    錯誤再分四種：不該出卡卻出卡、不該澄清卻澄清、不該攔卻攔、該攔沒攔。

不做什麼
    - 不連資料庫、不重跑規則推薦、不生成回答：只量「路走對沒有」，回答品質要另外用 T4 跑。
    - 門檻用 route_gate.DEFAULT_THRESHOLD（實驗前定好）；另外列幾個門檻的結果只是描述，不拿來挑門檻。
    - 只看每題第一輪（多輪接續的路由由 _carry_question 處理，不在本實驗）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import route_gate
from .preferences import RuleParser

# 守門的 card 只有在現行規則推薦卡真的能涵蓋時才會出卡（紀錄裡 route.reason 是這個值）
CARD_COVERS_REASON = "card_covers_request"
# 描述用的門檻清單（0＝直接取最高機率，不設門檻）；不拿來挑門檻
SENSITIVITY_THRESHOLDS = (0.0, 0.5, 0.7, 0.9)


# 讀多份題目檔，合成 {題號: 第一輪題目}；同一題號出現多次時以先讀到的為準。
def load_questions(paths: list[Path]) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in paths:
        for q in json.loads(path.read_text(encoding="utf-8")):
            out.setdefault(q["id"], q["turns"][0])
    return out


# 依優先順序在幾個紀錄資料夾找某題的紀錄，回傳第一輪的 turn；找不到回 None。
def load_first_turn(record_dirs: list[Path], qid: str) -> dict | None:
    for d in record_dirs:
        path = d / f"{qid}_record.json"
        if path.exists():
            turns = json.loads(path.read_text(encoding="utf-8")).get("turns") or []
            return turns[0] if turns else None
    return None


# 把現行系統某一輪實際的結果換成四種路徑之一（card／qa／clarify／intercept）。
def current_path(turn: dict) -> str:
    status = turn.get("status")
    route = turn.get("route") or {}
    if status == "needs_clarification":
        return "clarify"
    if status == "outside_scope":
        return "intercept"
    if route.get("use_qa"):
        return "qa"
    if status in ("recommended", "conditional", "no_match", "insufficient_evidence"):
        return "card"
    return f"other:{status}"
# 核心邏輯：看使用者實際收到什麼。商品題（G01、G03）雖然路由寫 outside_scope_or_product，
# 但交付的是推薦卡，所以算 card——這正是要修的問題，不能算成攔截。


# 規則攔截：現行規則判成範圍外或商品要求就攔下，回傳攔截原因；不攔回 None。
def rule_intercept(parser: RuleParser, text: str) -> str | None:
    parsed = parser.parse(text)
    intents = {parsed.intent, *parsed.secondary_intents}
    for name in ("outside_scope", "product_request"):
        if name in intents:
            return name
    return None


# 新系統的最後路徑：規則攔截優先；其次依守門結果，card 還要現行卡片真的能涵蓋才出卡。
def new_path(intercept: str | None, gate: route_gate.GateDecision | None, current_turn: dict | None) -> str:
    if intercept:
        return "intercept"
    if gate is None:
        return "qa"
    if gate.route == "card":
        reason = ((current_turn or {}).get("route") or {}).get("reason")
        return "card" if reason == CARD_COVERS_REASON else "qa"
    if gate.route == "outside":
        return "intercept"
    return gate.route
# 核心邏輯：守門只決定「可不可以用卡」，卡片內容仍由規則推薦負責；規則卡片沒涵蓋就退到問答（漏斗的保守方向）。


# 對照組「只改規則攔截」：規則判成範圍外或商品要求就攔下（商品不再出卡），其他維持現行實際走的路，不用模型。
def rules_only_path(intercept: str | None, current: str) -> str:
    return "intercept" if intercept else current
# 核心邏輯：用來拆開功勞——新系統比現行多對的題，有多少是規則攔截改動帶來的、有多少是模型守門帶來的。


# 依門檻重新判一次守門的路徑（用已存的四類機率，不重新呼叫模型）。
def gate_route_at(gate: route_gate.GateDecision, threshold: float) -> route_gate.GateDecision:
    if not gate.probs:
        return gate
    best = max(gate.probs, key=gate.probs.get)
    route = best if gate.probs[best] >= threshold else route_gate.FALLBACK_ROUTE
    return route_gate.GateDecision(route=route, label=gate.label, confidence=gate.confidence, probs=gate.probs,
                                   reason="argmax" if route == best else "low_confidence")


# 判斷錯誤種類：走的路不在可接受集合時，回傳錯誤名稱；走對回 None。
def error_kind(path: str, acceptable: list[str]) -> str | None:
    if path in acceptable:
        return None
    if "intercept" in acceptable and path != "intercept":
        return "該攔沒攔"
    return {"card": "不該出卡卻出卡", "clarify": "不該澄清卻澄清",
            "intercept": "不該攔卻攔", "qa": "該出卡卻問答"}.get(path, f"其他:{path}")
# 核心邏輯：「該攔沒攔」優先（安全面最重要）；qa 只有在標準答案不含 qa 時才算錯（目前沒有這種題）。


# 跑全部題目：規則攔截、守門判斷（攔截的題也跑守門，供參考），組成每題一列的結果。
def run(labels: dict, questions: dict[str, str], record_dirs: list[Path],
        config: route_gate.GateConfig) -> list[dict]:
    parser = RuleParser()
    rows: list[dict] = []
    for qid, spec in labels.items():
        text = questions.get(qid)
        if text is None:
            rows.append({"qid": qid, "error": "question_not_found"})
            continue
        turn = load_first_turn(record_dirs, qid)
        intercept = rule_intercept(parser, text)
        gate = route_gate.classify(text, config)
        cur = current_path(turn) if turn else "missing_record"
        new = new_path(intercept, gate, turn)
        rows.append({
            "qid": qid, "acceptable": spec["acceptable"], "why": spec["why"],
            "current": {"path": cur, "intent": (turn or {}).get("intent"),
                        "route_reason": ((turn or {}).get("route") or {}).get("reason"),
                        "error": error_kind(cur, spec["acceptable"])},
            "new": {"path": new, "rule_intercept": intercept, "gate": gate.to_dict(),
                    "error": error_kind(new, spec["acceptable"])},
        })
        add_rules_only(rows[-1])
        print(f"{qid:10s} 現行 {cur:9s} 新 {new:9s} 守門 {gate.route:8s} "
              f"信心 {gate.confidence} {gate.reason} {gate.latency_ms}ms", flush=True)
    return rows
# 核心邏輯：每題獨立判斷、立即印一行進度；守門失敗不中斷（classify 已把失敗轉成退到問答並記 error）。


# 在一列結果補上對照組「只改規則攔截」的路徑與錯誤（舊結果檔重新計分時也用這個補）。
def add_rules_only(row: dict) -> None:
    if "new" not in row:
        return
    path = rules_only_path(row["new"]["rule_intercept"], row["current"]["path"])
    row["rules_only"] = {"path": path, "error": error_kind(path, row["acceptable"])}


# 統計一個系統的正確數與錯誤種類。
def tally(rows: list[dict], side: str) -> dict:
    valid = [r for r in rows if side in r]
    errors = Counter(r[side]["error"] for r in valid if r[side]["error"])
    return {"total": len(valid), "correct": sum(1 for r in valid if not r[side]["error"]),
            "errors": dict(errors), "wrong_ids": [r["qid"] for r in valid if r[side]["error"]]}


# 描述用：在幾個門檻下，新系統各有幾題走對（不重新呼叫模型）。
def sensitivity(rows: list[dict]) -> list[dict]:
    out = []
    for th in SENSITIVITY_THRESHOLDS:
        correct = 0
        for r in rows:
            if "new" not in r:
                continue
            gate = route_gate.GateDecision(**r["new"]["gate"])
            path = new_path(r["new"]["rule_intercept"], gate_route_at(gate, th), _turn_stub(r))
            correct += error_kind(path, r["acceptable"]) is None
        out.append({"threshold": th, "correct": correct})
    return out


# 從結果列還原 new_path 需要的最少紀錄資訊（只用到 route.reason）。
def _turn_stub(row: dict) -> dict:
    return {"route": {"reason": row["current"]["route_reason"]}}


# 逐題表的一格：走錯的路前面加 ✗。
def _mark(row: dict, side: str) -> str:
    return ("✗ " if row[side]["error"] else "") + row[side]["path"]


# 寫 Markdown 摘要：總表、錯誤種類、逐題對照、門檻描述、耗時與信心分布。
def render_md(meta: dict, rows: list[dict]) -> str:
    cur, new, rules = tally(rows, "current"), tally(rows, "new"), tally(rows, "rules_only")
    gates = [r["new"]["gate"] for r in rows if "new" in r]
    lat = sorted(g["latency_ms"] for g in gates if g.get("latency_ms") is not None)
    conf = [g["confidence"] for g in gates if g.get("confidence") is not None]
    lines = [f"# 路由守門實驗結果（{meta['model']}，門檻 {meta['threshold']}）", "",
             f"- 執行時間：{meta['started_at']}～{meta['finished_at']}（UTC）",
             f"- 守門版本：{route_gate.GATE_VERSION}；標註：{meta['labels_version']}；題數：{cur['total']}",
             f"- 守門失敗（退到問答）：{sum(1 for g in gates if g['reason'] == 'gate_unavailable')} 題", "",
             "| 系統 | 走對 | 錯誤種類 | 走錯的題 |", "|---|---|---|---|",
             f"| 現行 route-v2 | {cur['correct']}／{cur['total']} | {cur['errors'] or '無'} | {'、'.join(cur['wrong_ids']) or '無'} |",
             f"| 對照：只改規則攔截（不用模型） | {rules['correct']}／{rules['total']} | {rules['errors'] or '無'} | "
             f"{'、'.join(rules['wrong_ids']) or '無'} |",
             f"| 規則攔截＋守門 | {new['correct']}／{new['total']} | {new['errors'] or '無'} | {'、'.join(new['wrong_ids']) or '無'} |",
             "", "## 門檻描述（不拿來挑門檻）", "", "| 門檻 | 走對 |", "|---|---|"]
    lines += [f"| {s['threshold']} | {s['correct']}／{new['total']} |" for s in sensitivity(rows)]
    lines += ["", "## 耗時與信心", "",
              f"- 守門耗時（毫秒）：中位數 {lat[len(lat) // 2] if lat else '無'}、最大 {lat[-1] if lat else '無'}",
              f"- 信心（softmax 最高機率）：最低 {min(conf) if conf else '無'}、"
              f"低於 0.9 的有 {sum(1 for c in conf if c < 0.9)} 題",
              "", "## 逐題", "", "| 題 | 可接受 | 現行 | 只改規則 | 新 | 規則攔截 | 守門 | 信心 | 四類機率 |",
              "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if "new" not in r:
            lines.append(f"| {r['qid']} | {r.get('error')} | | | | | | | |")
            continue
        g = r["new"]["gate"]
        lines.append(f"| {r['qid']} | {'／'.join(r['acceptable'])} | {_mark(r, 'current')} | "
                     f"{_mark(r, 'rules_only')} | {_mark(r, 'new')} | "
                     f"{r['new']['rule_intercept'] or ''} | {g['route']}（{g['reason']}） | {g['confidence']} | "
                     f"{json.dumps(g['probs'], ensure_ascii=False)} |")
    return "\n".join(lines) + "\n"


# 重新計分：讀既有結果檔（.json），補上對照組、重算摘要，重寫 .json 與 .md；不呼叫模型。
def rescore(result_json: Path) -> dict:
    data = json.loads(result_json.read_text(encoding="utf-8"))
    for row in data["rows"]:
        add_rules_only(row)
    data["meta"]["summary"] = {"current": tally(data["rows"], "current"), "new": tally(data["rows"], "new"),
                               "rules_only": tally(data["rows"], "rules_only"),
                               "sensitivity": sensitivity(data["rows"])}
    result_json.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    result_json.with_suffix(".md").write_text(render_md(data["meta"], data["rows"]), encoding="utf-8")
    return data["meta"]["summary"]
# 核心邏輯：計分規則改了（例如加對照組）時，用已存的模型機率重算，不必再花時間、token 重跑模型。


# 命令列入口：讀設定與資料、跑全部題目、寫 JSON 與 Markdown；給 --rescore 時只重新計分。
def main(argv: list[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv[1:]
    if argv[:1] == ["--rescore"]:
        sys.stdout.reconfigure(encoding="utf-8")
        for path in argv[1:]:
            print(path, json.dumps(rescore(Path(path)), ensure_ascii=False), flush=True)
        return
    ap = argparse.ArgumentParser(prog="python -m app.route_gate_eval")
    ap.add_argument("--labels", type=Path, required=True)
    ap.add_argument("--questions", type=Path, nargs="+", required=True)
    ap.add_argument("--records", type=Path, nargs="+", required=True, help="紀錄資料夾，依優先順序")
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--api-key-env", default=None, help="放金鑰的環境變數名稱（金鑰不寫進輸出）")
    ap.add_argument("--threshold", type=float, default=route_gate.DEFAULT_THRESHOLD)
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--out", type=Path, required=True, help="輸出檔名前綴（會寫 .json 與 .md）")
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    labels = json.loads(a.labels.read_text(encoding="utf-8"))
    config = route_gate.GateConfig(base_url=a.base_url.rstrip("/"), model=a.model,
                                   api_key=os.environ.get(a.api_key_env) if a.api_key_env else None,
                                   timeout_s=a.timeout, threshold=a.threshold)
    host = a.base_url.split("/")[2] if "//" in a.base_url else a.base_url
    meta = {"started_at": datetime.now(timezone.utc).isoformat(), "model": a.model,
            # 遠端通道網址視同秘密（同 run_31.py 的做法），只記本機或遠端
            "endpoint": "local" if host.startswith(("127.0.0.1", "localhost")) else "remote",
            "threshold": a.threshold, "labels_version": labels["version"], "gate_version": route_gate.GATE_VERSION}
    rows = run(labels["labels"], load_questions(a.questions), a.records, config)
    meta["finished_at"] = datetime.now(timezone.utc).isoformat()
    meta["summary"] = {"current": tally(rows, "current"), "new": tally(rows, "new"),
                       "rules_only": tally(rows, "rules_only"), "sensitivity": sensitivity(rows)}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.with_suffix(".json").write_text(json.dumps({"meta": meta, "rows": rows}, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
    a.out.with_suffix(".md").write_text(render_md(meta, rows), encoding="utf-8")
    print("ROUTE_GATE_EVAL_DONE", json.dumps(meta["summary"], ensure_ascii=False), flush=True)
# 核心邏輯：網址只記主機名稱、金鑰只從環境變數讀進記憶體，兩者都不寫進輸出檔。


if __name__ == "__main__":
    main()
