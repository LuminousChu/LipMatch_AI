"""關係選段（relation-select-v1）：QA_SELECTOR=relation 時，依關係包從本次候選中選出「已核准、條件適用」的直接支持片段。

確定性程式，不呼叫任何模型。關係包（relation-pack-v1）由 G1 分支 export_relation_pack.py 產生，放在 Git 外，
路徑與 SHA 由 QA_RELATION_PACK／QA_RELATION_PACK_SHA256 指定；本模組只讀它，不寫資料庫。

流程（不讀題號、不讀評分資料）：
  1. 需求概念：問題比對 need_cues（主樣式＋必要共現樣式）；沒有命中＝不做關係選段。
  2. 使用者條件：問題比對 profile_cues，同一維度先比先贏；每項附命中原文。
  3. 比對每個候選片段的 recall_records：
       - 紀錄的 unit_text_sha256 必須等於候選片段原文的 SHA（關係包綁定的就是資料庫這段文字）；
       - addresses 概念在 accept[需求] 內；targets 與 targets_ok 相容；方向相反（conflict）排除；商品宣稱排除；
       - applies_when 以整棵 all／any／not 條件樹判三值：衝突排除，未知保留並標示。
  4. 排序：條件（符合 → 原文未附條件 → 未知）→ 明示先於推導 → 直接支持先於成因說明 → method.kind → 向量名次；
     同片段只取一次，最多 max_keep 個。
  5. 回傳 kept（可能為空）與逐筆判定；kept 為空時由呼叫端維持既有（selector=none）流程。
另提供 condition_notes：把解析到、但使用者條件清單沒有的維度寫成 contract-v1 的 notes 項目（附原話），
讓送進模型的使用者條件與證據適用判定出自同一份解析。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

VERSION = "relation-select-v1"
COND_RANK = {"符合": 0, "原文未附條件": 1, "未知": 2}
STATUS_ZH = {"true": "符合", "false": "衝突", "unknown": "未知"}
DIM_ZH = {"undertone": "底調", "blush": "腮紅", "eye_makeup": "眼妝", "lip_natural_color": "原生唇色", "goal": "希望效果",
          "finish": "妝感", "lip_state": "嘴唇狀態"}
VAL_ZH = {"cool": "冷", "warm": "暖", "neutral": "中性", "heavy": "加重", "smoky_heavy": "濃煙燻", "smoky": "煙燻",
          "deep": "偏深", "light": "淡", "minimal": "淡", "not_heavy": "不要太重", "understated": "低調", "matte": "霧面",
          "dry_chapped": "乾燥"}
C_FIELDS = {"undertone": "undertone"}       # 使用者條件清單（contract-v1）已有的欄位；其餘寫進 notes


class PackError(RuntimeError):
    """關係包不存在、SHA 不符或格式不對。"""


def load_pack(path: str | Path, expected_sha256: str | None = None) -> dict:
    """讀關係包並核對 SHA（有指定時）；版本不是 relation-pack-v1 就報錯。"""
    p = Path(path)
    if not p.is_file():
        raise PackError(f"關係包不存在：{p}")
    raw = p.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    if expected_sha256 and sha != expected_sha256.lower():
        raise PackError("關係包 SHA 不符")
    pack = json.loads(raw.decode("utf-8"))
    if pack.get("version") != "relation-pack-v1":
        raise PackError(f"不支援的關係包版本：{pack.get('version')}")
    pack["_sha256"] = sha
    return pack


def need_concepts(question: str, pack: Mapping[str, Any]) -> list[dict]:
    """需求概念：主樣式命中，且有共現樣式時也要命中；同概念只列一次，附命中原文。"""
    out, seen = [], set()
    for cue in pack["need_cues"]:
        m = re.search(cue["pattern"], question)
        if m and cue["concept"] not in seen and (not cue.get("require") or re.search(cue["require"], question)):
            out.append({"concept": cue["concept"], "matched": m.group(0)})
            seen.add(cue["concept"])
    return out


def profile(question: str, pack: Mapping[str, Any]) -> dict:
    """使用者條件：同維度先比先贏；回傳 {values, hits}（hits 附命中原文）。"""
    values, hits = {}, []
    for cue in pack["profile_cues"]:
        if cue["dim"] in values:
            continue
        m = re.search(cue["pattern"], question)
        if m:
            values[cue["dim"]] = cue["value"]
            hits.append({"dim": cue["dim"], "value": cue["value"], "matched": m.group(0)})
    return {"values": values, "hits": hits}


def _compatible(dim: str, have: str, need: str, pack: Mapping[str, Any]) -> bool:
    """使用者值是否滿足條件值（例：濃煙燻滿足眼妝加重）。"""
    if have == need:
        return True
    return any(s["dim"] == dim and s["have"] == have and need in s["satisfies"] for s in pack.get("satisfies") or [])


def eval_cond(node: Mapping[str, Any] | None, values: Mapping[str, str], pack: Mapping[str, Any]) -> str:
    """整棵條件樹三值判定（all／any／not；葉節點比對使用者條件）。"""
    if not node:
        return "true"
    if "all" in node:
        vals = [eval_cond(c, values, pack) for c in node["all"] if c.get("role") != "suitability_note"]
        return "false" if "false" in vals else ("unknown" if "unknown" in vals else "true")
    if "any" in node:
        vals = [eval_cond(c, values, pack) for c in node["any"] if c.get("role") != "suitability_note"]
        if not vals:
            return "true"
        return "true" if "true" in vals else ("unknown" if "unknown" in vals else "false")
    if "not" in node:
        return {"true": "false", "false": "true", "unknown": "unknown"}[eval_cond(node["not"], values, pack)]
    have = values.get(node["dim"])
    if have is None:
        return "unknown"
    return "true" if _compatible(node["dim"], have, node["eq"], pack) else "false"


def _leaves(node: Mapping[str, Any] | None) -> list[dict]:
    """條件樹的葉節點清單。"""
    if not node:
        return []
    for k in ("all", "any"):
        if k in node:
            return [lf for c in node[k] for lf in _leaves(c)]
    if "not" in node:
        return _leaves(node["not"])
    return [dict(node)]


def match_record(rec: Mapping[str, Any], concepts: Sequence[str], values: Mapping[str, str], pack: Mapping[str, Any]) -> dict:
    """單筆紀錄對需求：可否納入、明示／推導、角色、條件三值與原因。"""
    out = {"recall_id": rec["recall_id"], "eligible": False, "reason": None, "basis": None, "role": "直接支持",
           "condition": None, "leaves": []}
    hits = []
    for need in concepts:
        for a in rec["addresses"]:
            if a["concept"] in (pack.get("conflict") or {}).get(need, []):
                out["reason"] = f"方向相反：{a['concept']}"
                return out
            acc = (pack["accept"].get(need) or {}).get(a["concept"])
            if acc:
                hits.append((need, a, acc))
    if not hits:
        out["reason"] = "addresses 概念不對應需求"
        return out
    need, a, acc = min(hits, key=lambda h: pack["basis_order"][h[2]["basis"]])
    out.update(basis=acc["basis"], role=acc.get("role") or "直接支持", need=need, address_quote=a["quote"])
    if not set(rec["targets"]) & set(pack["targets_ok"].get(need, [])):
        out["reason"] = "對象不符"
        return out
    leaves = _leaves(rec.get("applies_when"))
    cond = eval_cond(rec.get("applies_when"), values, pack)
    out["condition"] = STATUS_ZH[cond] if leaves else "原文未附條件"
    out["leaves"] = [{"dim": lf["dim"], "eq": lf["eq"], "quote": lf["quote"],
                      "user": STATUS_ZH[eval_cond(lf, values, pack)]} for lf in leaves]
    if rec.get("product_specific"):
        out["reason"] = "商品宣稱"
        return out
    if cond == "false":
        out["reason"] = "條件與使用者衝突"
        return out
    out["eligible"] = True
    return out


def select(question: str, candidates: Sequence[Any], pack: Mapping[str, Any], *, max_keep: int = 2) -> dict:
    """候選 → 已核准、條件適用的直接支持片段（最多 max_keep）；候選可為 dict 或有 unit_id／text／pool_rank 屬性的物件。"""
    def get(c, k):
        return c.get(k) if isinstance(c, Mapping) else getattr(c, k, None)

    concepts = need_concepts(question, pack)
    prof = profile(question, pack)
    names = [c["concept"] for c in concepts]
    by_unit: dict[str, list] = {}
    for r in pack["records"]:
        by_unit.setdefault(r["unit_id"], []).append(r)
    kind_order = {k: i for i, k in enumerate(pack["kind_order"])}
    rows = []
    if names:
        for c in candidates:
            uid, text = get(c, "unit_id"), get(c, "text") or ""
            for rec in by_unit.get(uid, []):
                m = match_record(rec, names, prof["values"], pack)
                sha_ok = hashlib.sha256(text.encode("utf-8")).hexdigest() == rec["unit_text_sha256"]
                if m["eligible"] and not sha_ok:
                    m["eligible"], m["reason"] = False, "資料庫片段原文與關係包綁定的原文不同"
                rows.append({**m, "unit_id": uid, "pool_rank": get(c, "pool_rank"), "kind": rec["method_kind"],
                             "evidence_level": rec["evidence_level"], "approval": rec["approval"]["decision_id"],
                             "not_supported": rec["not_supported"]})
    elig = sorted((r for r in rows if r["eligible"]),
                  key=lambda r: (COND_RANK.get(r["condition"], 9), pack["basis_order"].get(r["basis"], 9),
                                 r["role"] != "直接支持", kind_order.get(r["kind"], 9), r["pool_rank"] or 10 ** 9, r["recall_id"]))
    kept = []
    for r in elig:
        if r["unit_id"] in kept:
            r["decision"] = "同片段已選"
        elif len(kept) >= max_keep:
            r["decision"] = f"超過名額 {max_keep}"
        else:
            r["decision"] = "選入"
            kept.append(r["unit_id"])
    for r in rows:
        r.setdefault("decision", "不選：" + (r["reason"] or ""))
    # 方法組（pack["method_groups"]，method-block-app-v1）：選入的紀錄有方法組時，組內片段一起選入（入口在前），
    # 只從本次候選中取；組內有片段不在候選裡就不展開，交給方法區塊拒收並攔下引用該組的模型句子。
    method_group = None
    groups = pack.get("method_groups") or {}
    entry = next((r for r in rows if r.get("decision") == "選入" and r.get("recall_id") in groups), None)
    if entry is not None:
        g = groups[entry["recall_id"]]
        cand_ids = {get(c, "unit_id") for c in candidates}
        missing = [u for u in g.get("units") or [] if u not in cand_ids]
        if g.get("exportable") and not missing:
            kept = list(dict.fromkeys(kept + list(g["units"])))
            method_group = {"entry": entry["recall_id"], "status": "expanded", "units": list(g["units"])}
        else:
            method_group = {"entry": entry["recall_id"], "status": "not_expanded", "missing_units": missing,
                            "reason": None if g.get("exportable") else g.get("reason")}
    return {"version": VERSION, "pack_version": pack["version"], "pack_sha256": pack.get("_sha256"),
            "need_concepts": concepts, "profile": prof, "kept": kept, "rows": rows, "method_group": method_group,
            "status": "selected" if kept else ("no_need_concept" if not names else "no_applicable_record")}


def condition_notes(prof: Mapping[str, Any], question: str, explicit_conditions: Sequence[Mapping[str, Any]]) -> list[dict]:
    """解析到的使用者條件中，使用者條件清單還沒有的維度 → contract-v1 notes 項目（值為中文、raw 為原話所在小句）。"""
    have = {(c.get("field"), c.get("value")) for c in explicit_conditions}
    out = []
    for h in prof["hits"]:
        field = C_FIELDS.get(h["dim"])
        if field and (field, h["value"]) in have:
            continue
        i = question.find(h["matched"])
        s = max(question.rfind(ch, 0, i) for ch in "，。？！、") + 1
        ends = [j for j in (question.find(ch, i + len(h["matched"])) for ch in "，。？！、") if j >= 0]
        raw = question[s:min(ends) if ends else len(question)]
        zh = f"{DIM_ZH.get(h['dim'], h['dim'])}：{VAL_ZH.get(h['value'], h['value'])}"
        out.append({"field": field or "notes", "value": h["value"] if field else zh, "values": None, "label": None,
                    "raw": raw, "source": VERSION})
    return out
# 核心邏輯：需求概念、使用者條件與證據適用判定都只由問題文字和關係包決定；候選片段原文必須和關係包綁定的 SHA 相同；
# 沒有可選片段時回傳空 kept，由呼叫端維持既有流程（不硬選、不降級成未核准片段）。
