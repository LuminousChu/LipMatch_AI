"""意圖判斷追蹤（intent-trace-v1）：把 RuleParser 判斷一則訊息意圖的過程逐步攤開，找出結構問題。

為什麼有這個工具（2026-11-01）：要找出 40 題裡意圖判斷有哪些結構問題，
以及它們對接的處理程序。
    RuleParser.parse() 只回傳結果（意圖、偏好變更、衝突），不說是哪一條規則讓它變成這個意圖；
    路由又再依意圖與推薦結果判一次路。這個工具不改任何判斷，只把中間訊號記下來，
    再和跑測紀錄（實際走了哪條路、接到哪段處理）對起來。

判斷順序（照抄 preferences.RuleParser._classify，preferences.py:743-766；改規則時這裡要一起改，
tests/test_intent_trace.py 會檢查兩邊結果一致）：

    ① 掃詞與個人描述 → has_changes（有沒有「條件」：想要／不要、底調、膚色、場合、過敏、模糊詞…）
    ② 依序收集意圖：範圍外 → 商品 → 比較 → 推薦／修改（看 has_changes 與提問詞）→ 知識 → 推薦
    ③ 第一個收集到的是主要意圖，其他是次要意圖
    ④ 主要或次要意圖含推薦／修改時，「只是提到」的色系、妝感詞改成「想要」（意圖反過來決定偏好）
    ⑤ 同一則訊息同一個鍵同時想要與不要 → 句內衝突

結構旗標（每題可多個；代號對應 docs/mvp/handoffs/2026-11-01_40題意圖判斷結構問題.md）：
    S1 預設倒向推薦：有條件、沒對到任何提問詞，就判推薦或修改（不是因為使用者要推薦）
    S2 問句沒被認出：訊息是問句（？、嗎、呢、怎麼…），提問詞清單卻沒有命中
    S3 意圖改寫偏好：「只是提到」的詞因為意圖是推薦／修改，被改成「想要」（rule-v15 起只限同一子句明說在找）
    S4 多個需求只給一個標籤：訊息有多子需求訊號，意圖仍只有一個主要標籤
    S5 只看有沒有問顏色，不看問什麼東西：推薦詞命中，但整則訊息沒有唇彩詞
    S6 衝突來源可疑：句內衝突的其中一邊來自「只是提到」，或否定字是別的詞的一部分（例如「分別」）
    S7 轉述被當成條件：條件來自「很多人說／都說／聽說…」這類轉述別人說法的子句，不是使用者自己的需求

不做什麼：不改解析器、不呼叫模型、不連資料庫；結構旗標是訊號，不是對錯判讀。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import preferences as pref

TRACE_VERSION = "intent-trace-v1"

# 問句訊號：用來判斷「這是不是一個問題」，和提問詞清單分開（S2 就是在找兩者不一致的地方）
_RE_QUESTION_FORM = re.compile(r"[？?]|嗎|呢|怎麼|如何|為什麼|是不是|會不會|哪|什麼|有沒有|能不能|可不可以")
# 多子需求訊號：一則訊息要求兩件以上的事
_RE_MULTI_PART = re.compile(r"兩種|兩個|兩件|分別|各自|另一種|一種.{0,20}另一種|以及|還有.{0,12}[？?嗎]|同時.{0,10}和|再.{0,6}怎麼")
# 一則訊息裡的「問句」數：以問號或句尾問詞斷句，計算帶問句訊號的段數（兩段以上＝多個問題）
_RE_SENTENCE_SPLIT = re.compile(r"[。！？?!；;\n]+")
# 唇彩訊號（比 preferences._RE_LIP_ITEM 寬）：唇彩詞，或「擦／塗」這類上唇動作；色系詞另外看詞表
_RE_LIP_ANY = re.compile(r"唇色|唇彩|口紅|唇膏|唇釉|唇蜜|唇油|紅唇|唇妝|嘴唇|唇線|唇|擦|塗")
# 轉述別人的說法：子句裡的條件不是使用者自己的需求
_RE_HEARSAY = re.compile(r"很多人說|大家說|大家都說|都說|聽說|據說|網路上說|別人說|有人說")
# 交給 S2 檢查的意圖：被攔截的題不需要認出是不是問句
_INTERCEPTED = ("outside_scope", "product_request")
# 否定字其實是別的詞的一部分（「分別」的「別」、「特別」…）
_RE_NEG_INSIDE_WORD = re.compile(r"分別|個別|特別|區別|類別|性別|識別|差別|告別")


# 找出第一個命中的字串（沒有回 None），方便在報表裡看到是哪個字觸發。
def _hit(pattern: re.Pattern, text: str) -> str | None:
    m = pattern.search(text)
    return m.group(0) if m else None


# 重現 parse() 前半段：掃詞、模糊詞、個人描述、預算、過敏，算出 has_changes 與它的來源。
def _changes_and_flag(parser: pref.RuleParser, normalized: str) -> tuple[list[dict], bool, list[str]]:
    changes, occasion_conflicts, ambiguities = parser._scan_terms(normalized)
    ambiguities += parser._scan_brightness(normalized, changes)
    changes += parser._scan_profile(normalized)
    budget = parser._scan_budget(normalized)
    if budget:
        changes.append(budget)
    allergy = parser._scan_allergy(normalized)
    reasons = [f"{c['field']}:{c.get('key') or c.get('value')}（{c['op']}）" for c in changes if c["op"] != "mention"]
    if allergy:
        reasons.append("過敏")
    if ambiguities:
        reasons.append("模糊詞")
    if occasion_conflicts:
        reasons.append("場合衝突")
    vitality = _hit(pref._RE_VITALITY, normalized)
    if vitality:
        reasons.append(f"外觀目標「{vitality}」")
    return changes, bool(reasons), reasons
# 核心邏輯：和 parse() 用同一組私有方法，所以 has_changes 一定相同；另外記下「是哪些條件讓它成立」。


# 照 _classify 的順序收集意圖，並記下每一步是哪個字觸發（branch 是決定主要意圖的那一步）。
def classify_steps(normalized: str, has_changes: bool) -> dict:
    steps: list[dict] = []
    if pref._requests_restricted(normalized):        # rule-v14（I03）
        steps.append({"intent": "restricted_request", "rule": "索取非公開資料",
                      "hit": _hit(pref._RE_RESTRICTED_TARGET, normalized)})
    outside = pref.outside_hit(normalized)
    if outside:
        steps.append({"intent": "outside_scope", "rule": "範圍外詞", "hit": outside})
    product = pref.product_hit(normalized)
    if product:
        steps.append({"intent": "product_request", "rule": "商品詞、挑一支，或「品牌／色號＋挑選」", "hit": product})
    n_terms = len({pref._TOKEN_INDEX[m.group(0)][1][:2] for m in pref._TOKEN_RE.finditer(normalized)
                   if pref._TOKEN_INDEX[m.group(0)][0] == "term"
                   and pref._TOKEN_INDEX[m.group(0)][1][0] in ("color", "finish")})
    strong = _hit(pref._RE_COMPARE_STRONG, normalized)
    weak = _hit(pref._RE_COMPARE_WEAK, normalized)
    if strong or (weak and n_terms >= 2):
        steps.append({"intent": "compare", "rule": "比較句型", "hit": strong or f"{weak}＋{n_terms} 個色系／妝感詞"})
    update_word = _hit(pref._RE_UPDATE, normalized)
    rec_cue = _hit(pref._RE_RECOMMEND, normalized)
    know_cue = _hit(pref._RE_KNOWLEDGE, normalized)
    asks = bool(rec_cue or know_cue)
    lip_item = _hit(pref._RE_LIP_ITEM, normalized)
    # rule-v14（K1）：沒被提問詞認出的問句／指令句，不走「有條件就推薦／修改」的預設
    answer_req = _hit(pref._RE_ANSWER_REQUEST, normalized) if not asks and not update_word else None
    if has_changes and not update_word and not asks and not answer_req and lip_item:
        steps.append({"intent": "recommend", "rule": "有條件＋沒有提問詞＋有唇彩詞（預設推薦）", "hit": lip_item})
    elif has_changes and (update_word or (not asks and not answer_req)):
        steps.append({"intent": "update", "rule": "有條件＋（有修改詞或沒有提問詞）",
                      "hit": update_word or "沒有提問詞"})
    if know_cue:
        steps.append({"intent": "knowledge", "rule": "知識詞", "hit": know_cue})
    if rec_cue:
        steps.append({"intent": "recommend", "rule": "推薦詞", "hit": rec_cue})
    if not steps:
        steps.append({"intent": "recommend" if has_changes and not answer_req else "knowledge",
                      "rule": "什麼都沒命中（預設）", "hit": answer_req})
    seen: list[str] = []
    for s in steps:
        if s["intent"] not in seen:
            seen.append(s["intent"])
    return {"intent": seen[0], "secondary": seen[1:], "branch": steps[0], "steps": steps,
            "cues": {"recommend": rec_cue, "knowledge": know_cue, "update": update_word, "lip_item": lip_item,
                     "compare_strong": strong, "compare_weak": weak, "outside": outside, "product": product}}
# 核心邏輯：每一步都記「規則名稱＋觸發字」；branch 是第一個收集到的步驟，也就是主要意圖的來源。


# 追蹤一則訊息：重現意圖判斷、偏好改寫與衝突，並和 parse() 的結果核對，最後標結構旗標。
def trace(text: str, parser: pref.RuleParser | None = None) -> dict:
    parser = parser or pref.RuleParser()
    normalized = pref._RE_RESOLVE.sub(pref._resolve_phrase, text)
    changes, has_changes, why = _changes_and_flag(parser, normalized)
    cls = classify_steps(normalized, has_changes)
    parsed = parser.parse(text)
    wants = parsed.intent in ("recommend", "update") or "recommend" in parsed.secondary_intents
    mentions = [c for c in changes if c["op"] == "mention"]
    # rule-v15：只有同一子句明說在找（推薦、想找、幫我挑…）的提到詞才會變成想要
    promoted = [f"{c['field']}:{c['key']}「{c['raw']}」" for c in mentions
                if wants and pref._RE_SEEK_IN_CLAUSE.search(c.get("raw") or "")]
    flags: list[str] = []
    if cls["branch"]["rule"].startswith("有條件"):
        flags.append("S1")
    if (parsed.intent not in _INTERCEPTED and _RE_QUESTION_FORM.search(text)
            and not (cls["cues"]["recommend"] or cls["cues"]["knowledge"] or cls["cues"]["compare_strong"])):
        flags.append("S2")
    if promoted:
        flags.append("S3")
    questions = question_count(text)
    multi = _hit(_RE_MULTI_PART, text) or (f"{questions} 個問句" if questions >= 2 else None)
    if multi:
        flags.append("S4")
    has_term = any(pref._TOKEN_INDEX[m.group(0)][0] == "term" and pref._TOKEN_INDEX[m.group(0)][1][0]
                   in ("color", "finish") for m in pref._TOKEN_RE.finditer(text))
    if cls["cues"]["recommend"] and not _RE_LIP_ANY.search(text) and not has_term:
        flags.append("S5")
    if parsed.conflicts:
        raws = "／".join(c.get("raw") or "" for c in parsed.conflicts)
        if _RE_NEG_INSIDE_WORD.search(raws) or any(m["raw"] in raws for m in mentions):
            flags.append("S6")
    hearsay = [c for c in parsed.changes if c.get("raw") and _RE_HEARSAY.search(c["raw"])]
    if hearsay:
        flags.append("S7")
    return {
        "text": text, "normalized": normalized if normalized != text else None,
        "intent": parsed.intent, "secondary": parsed.secondary_intents,
        "reproduced_ok": (cls["intent"], cls["secondary"]) == (parsed.intent, parsed.secondary_intents),
        "branch": cls["branch"], "steps": cls["steps"], "cues": cls["cues"],
        "has_changes": has_changes, "has_changes_from": why,
        "promoted_mentions": promoted,
        "conditions": [f"{c['field']}:{c.get('key') or c.get('value')}「{c.get('raw')}」" for c in parsed.changes],
        "conflicts": [f"{c['field']}:{c.get('key')}「{c.get('raw')}」" for c in parsed.conflicts],
        "multi_part_cue": multi, "question_count": questions,
        "hearsay_conditions": [f"{c['field']}:{c.get('key') or c.get('value')}「{c['raw']}」" for c in hearsay],
        "flags": flags, "version": TRACE_VERSION,
    }
# 核心邏輯：
# 1. 意圖以 parse() 的實際結果為準；reproduced_ok 檢查本工具重現的順序是否一致（不一致代表規則改了、工具沒跟上）。
# 2. 結構旗標只是訊號：例如 S1 命中但使用者真的要推薦，路就沒走錯；對錯由人或標準答案判斷。


# 數一則訊息有幾個問句：斷句後，帶問句訊號（？、嗎、怎麼…）的段數。
def question_count(text: str) -> int:
    parts = [p for p in _RE_SENTENCE_SPLIT.split(text) if p.strip()]
    marks = text.count("？") + text.count("?")
    return max(marks, sum(1 for p in parts if _RE_QUESTION_FORM.search(p)))
# 核心邏輯：取「問號數」和「帶問詞的句段數」較大者；「會不會顯得蒼白？如果想擦紅色要怎麼挑？」算 2。


# 從跑測紀錄取出這題第一輪實際接到的處理程序（路由出口、推薦結果、檢索與生成、色名參照）。
def downstream(record_turn: dict | None) -> dict:
    if not record_turn:
        return {"route": None}
    route = record_turn.get("route") or {}
    rec = record_turn.get("recommendation") or {}
    qa_used = bool(route.get("use_qa"))
    return {
        "route": route.get("reason"), "use_qa": qa_used, "status": record_turn.get("status"),
        "rule_recommendation": rec.get("status") if rec else record_turn.get("rec_status"),
        "card_delivered": (not qa_used) and record_turn.get("status") in
                          ("recommended", "conditional", "no_match", "insufficient_evidence"),
        "retrieval_pool": len(record_turn.get("pool") or []), "sent": len(record_turn.get("sent") or []),
        "generation_calls": record_turn.get("generation_calls"),
        "color_reference": "色名參照" in (record_turn.get("answer") or ""),
        "seconds": record_turn.get("wall_s"),
    }
# 核心邏輯：card_delivered＝沒有走問答、而且交付的是推薦卡（含 no_match 卡）；這正是「推薦卡搶答」要看的欄位。


# 讀題目檔與紀錄資料夾，逐題（含多輪的每一輪）追蹤；回傳結果列。
def run(questions_path: Path, record_dir: Path) -> list[dict]:
    parser = pref.RuleParser()
    rows = []
    for q in json.loads(questions_path.read_text(encoding="utf-8")):
        rec_path = record_dir / f"{q['id']}_record.json"
        turns = json.loads(rec_path.read_text(encoding="utf-8"))["turns"] if rec_path.exists() else []
        for i, text in enumerate(q["turns"]):
            row = {"qid": q["id"] + (f"#t{i + 1}" if len(q["turns"]) > 1 else ""), "category": q["category"],
                   **trace(text, parser), "downstream": downstream(turns[i] if i < len(turns) else None)}
            row["recorded_intent"] = (turns[i] if i < len(turns) else {}).get("intent")
            rows.append(row)
    return rows
# 核心邏輯：多輪題的第二輪在系統裡會接上上一輪（_carry_question、followup），這裡只追蹤單句解析，
# 所以第二輪的 recorded_intent 可能和單句解析不同，報表會標出來。


# 寫 Markdown：旗標統計、逐題表（意圖來源、觸發字、改寫的偏好、衝突、旗標、實際路由與下游）。
def render_md(rows: list[dict]) -> str:
    from collections import Counter
    flags = Counter(f for r in rows for f in r["flags"])
    branches = Counter(r["branch"]["rule"] for r in rows)
    lines = [f"# 意圖判斷追蹤（{TRACE_VERSION}，{len(rows)} 輪）", "",
             f"- 重現與 parse() 一致：{sum(r['reproduced_ok'] for r in rows)}／{len(rows)}",
             f"- 單句解析意圖與跑測紀錄一致：{sum(r['intent'] == r['recorded_intent'] for r in rows)}／{len(rows)}",
             "", "## 主要意圖由哪條規則決定", "", "| 規則 | 輪數 |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in branches.most_common()]
    lines += ["", "## 結構旗標", "", "| 旗標 | 輪數 | 題 |", "|---|---|---|"]
    for f in ("S1", "S2", "S3", "S4", "S5", "S6", "S7"):
        ids = [r["qid"] for r in rows if f in r["flags"]]
        lines.append(f"| {f} | {len(ids)} | {'、'.join(ids) or '無'} |")
    lines += ["", "## 逐題", "",
              "| 題 | 意圖（次要） | 決定規則／觸發字 | 條件來源 | 提到詞改成想要 | 衝突 | 旗標 | 路由出口 | 下游 |",
              "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        d = r["downstream"]
        down = ("推薦卡" if d.get("card_delivered") else
                f"問答（池{d.get('retrieval_pool')}／送{d.get('sent')}）" if d.get("use_qa") else d.get("status"))
        if d.get("color_reference"):
            down += "＋色名參照"
        intent = r["intent"] + (f"（{'、'.join(r['secondary'])}）" if r["secondary"] else "")
        if r["recorded_intent"] and r["recorded_intent"] != r["intent"]:
            intent += f"；紀錄 {r['recorded_intent']}"
        lines.append(f"| {r['qid']} | {intent} | {r['branch']['rule']}／{r['branch']['hit'] or '—'} | "
                     f"{'、'.join(r['has_changes_from']) or '—'} | {'、'.join(r['promoted_mentions']) or '—'} | "
                     f"{'、'.join(r['conflicts']) or '—'} | {'、'.join(r['flags']) or '—'} | {d.get('route')} | {down} |")
    return "\n".join(lines) + "\n"


# 命令列入口：python -X utf8 -m app.intent_trace --questions <題目.json> --records <紀錄資料夾> --out <前綴>
def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m app.intent_trace")
    ap.add_argument("--questions", type=Path, required=True)
    ap.add_argument("--records", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True, help="輸出檔名前綴（寫 .json 與 .md）")
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    rows = run(a.questions, a.records)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.with_suffix(".json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    a.out.with_suffix(".md").write_text(render_md(rows), encoding="utf-8")
    print("INTENT_TRACE_DONE", len(rows), "輪；重現一致", sum(r["reproduced_ok"] for r in rows), flush=True)


if __name__ == "__main__":
    main()
