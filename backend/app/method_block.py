"""結構化方法呈現（method-block-app-v1）：關係選段選到「有方法組」的紀錄時，由程式依已核准的標註組出完整方法區塊，
取代模型對這組證據寫的句子。不呼叫任何模型。

方法組在匯出關係包時就組好（G1 context_group.assemble_group＋method_block.build_block，段落已換算到已核准的
資料庫片段），放在 pack["method_groups"][入口紀錄]；本模組只在請求時做三件事：
  1. 核對：組內每個必要片段都已送進本次回答、原文 SHA 等於組綁定的 SHA；任何一段沒到或不同 → 拒收。
  2. 條件：每個條件用整棵樹（relation_select.eval_cond）依本次解析到的使用者條件判定；
     必需條件衝突 → 拒收；非必需段衝突 → 只拿掉那段；未知 → 照出並在第一行明示。
  3. 顯示：方法區塊（適用情境 → 材料限定 → 依序步驟 → 目的 → 使用說明 → 支持界線，每項附來源）＋
     不引用組內片段的模型句子。組被採用或被拒收時，引用組內片段的模型句子一律不顯示（混合引用整條攔下），
     不讓模型版本的方法繞過拒收（G1 第九批之八補充指出的缺陷）。沒有方法組時不介入。
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from app import relation_select as RS

VERSION = "method-block-app-v1"
HEADER = "【結構化證據組裝】以下方法由程式依已核准的來源標註組成，不是模型生成。"
PRODUCT_Q = re.compile(r"商品|產品|品牌|色號|購買|推薦|哪一支|哪支")
STATUS_ZH = {"true": "符合", "false": "衝突", "unknown": "未知"}
ITEM_KEYS = ("applies_context", "background", "prep", "material", "steps", "application", "purpose",
             "usage_note")   # 方法區塊中「逐項附來源」的欄位


def _get(obj: Any, name: str, default: Any = None) -> Any:
    return obj.get(name, default) if isinstance(obj, Mapping) else getattr(obj, name, default)


def _src(unit_id: str) -> str:
    return f"〔來源 {unit_id}〕"


def entry_of(selection: Mapping[str, Any], pack: Mapping[str, Any]) -> str | None:
    """本次選入、而且有方法組的第一筆紀錄。"""
    groups = pack.get("method_groups") or {}
    for r in selection.get("rows") or []:
        if r.get("decision") == "選入" and r.get("recall_id") in groups:
            return r["recall_id"]
    return None


def render(block: Mapping[str, Any], comp: Mapping[str, Any], conds: Sequence[Mapping[str, Any]],
           cond_status: str) -> list[str]:
    lines = [HEADER, f"證據層級：{block.get('evidence_level')}"]
    if not comp.get("complete"):
        lines.append("資料不完整，不能當成完整做法；缺：" + "；".join(comp.get("missing") or []))
    if cond_status == "未知":
        lines.append("條件未知：下列方法只適用於原文所說的情況，請先確認您是否符合。")
    if block.get("applies_context"):
        lines.append("適用情境：")
        for c in conds:
            for q in c.get("quotes") or []:
                lines.append(f"  原文條件「{q}」，對您：{c['user_status']}")
        lines += [f"  原文：“{x['quote']}”{_src(x['unit_id'])}" for x in block["applies_context"]]
    if block.get("background"):
        lines.append("為什麼會這樣（作者說法）：")
        lines += [f"  原文：“{x['quote']}”{_src(x['unit_id'])}" for x in block["background"]]
    if block.get("prep"):
        lines.append("事前準備：")
        lines += [f"  原文：“{x['quote']}”{_src(x['unit_id'])}" for x in block["prep"]]
    if block.get("material"):
        lines.append("材料限定：")
        lines += [f"  原文：“{x['quote']}”{_src(x['unit_id'])}" for x in block["material"]]
    if block.get("steps"):
        lines.append("步驟（依原文順序）：")
        for st in block["steps"]:
            opt = "（可選）" if st.get("required") == "optional" and "可選" not in (st.get("action") or "") else ""
            when = f"；做這步的條件：“{st['when_quote']}”" if st.get("when_quote") else ""
            heading = "（原文為小節標題）" if st.get("quote_source") == "section" else ""
            lines.append(f"  {st['order']}. {st.get('action')}{opt}（標註者轉述）— 原文：“{st['quote']}”{heading}{when}"
                         f"{_src(st['unit_id'])}")
    if block.get("application"):
        lines.append("擦法：")
        lines += [f"  原文：“{x['quote']}”{_src(x['unit_id'])}" for x in block["application"]]
    if block.get("purpose"):
        lines.append("目的（作者說法，不是效果保證）：")
        lines += [f"  原文：“{x['quote']}”{_src(x['unit_id'])}" for x in block["purpose"]]
    if block.get("usage_note"):
        lines.append("使用說明（不是方法效果）：")
        lines += [f"  原文：“{x['quote']}”{_src(x['unit_id'])}" for x in block["usage_note"]]
    if block.get("boundaries"):
        lines.append("支持界線：" + "；".join(block["boundaries"]))
    return lines


def render_brief(brief: Mapping[str, Any], cond_status: str) -> list[str]:
    """brief-v1（使用者 2026-10-25 真人判定）：一句結論＋中文做法＋必要時一句提醒；不列原文、不加來源標記。"""
    lines = [brief["conclusion"]]
    if cond_status == "未知":
        lines.append("（這個做法適用原文說的情況，請先確認是否符合。）")
    lines.append("做法：")
    lines += [f"{i}. {s}" for i, s in enumerate(brief.get("steps") or [], start=1)]
    if brief.get("note"):
        lines.append(brief["note"])
    return lines


def assemble(selection: Mapping[str, Any], pack: Mapping[str, Any], claims: Sequence[Any],
             limitations: Sequence[str], clarifying_question: str | None, sent_sha: Mapping[str, str], *,
             product_feature: bool = False) -> dict:
    """回傳 {status: passthrough｜complete｜incomplete｜rejected, lines, cited_units, kept_claims, ...}。
    sent_sha：本次實際送進回答的片段 {unit_id: 原文 SHA}（呼叫端由固定映射取得）。"""
    entry = entry_of(selection, pack)
    if entry is None:
        return {"version": VERSION, "status": "passthrough", "reason": "沒有選到有方法組的紀錄"}
    g = pack["method_groups"][entry]
    values = (selection.get("profile") or {}).get("values") or {}
    reason, dropped = None, []
    conds = []
    for c in g.get("conditions") or []:
        v = RS.eval_cond(c["tree"], values, pack)
        conds.append({**c, "verdict": v, "user_status": STATUS_ZH[v]})
        if v == "false" and c.get("required"):
            reason = "條件與您的狀況衝突，不提供此方法"
        elif v == "false":
            dropped.append((c.get("unit_id"), c.get("role")))
    if not g.get("exportable"):
        reason = reason or f"方法組無法使用：{g.get('reason')}"
    missing = [u for u in g.get("required_units") or [] if u not in sent_sha]
    changed = [u for u in g.get("required_units") or [] if u in sent_sha and sent_sha[u] != g["unit_text_sha256"].get(u)]
    if missing:
        reason = reason or "方法組的必要片段沒有送達：" + "、".join(missing)
    if changed:
        reason = reason or "方法組的片段原文與核准標註不同：" + "、".join(changed)
    group_units = set(g.get("units") or []) | {g.get("entry_unit")}
    kept = [c for c in claims if not (set(_get(c, "unit_ids") or []) & group_units)]
    taken = [{"text": _get(c, "text"), "unit_ids": list(_get(c, "unit_ids") or [])} for c in claims if c not in kept]
    q = clarifying_question
    q_note = None
    if q and not product_feature and PRODUCT_Q.search(q):
        q, q_note = None, "商品功能關閉：追問涉及商品，不顯示"
    req_status = {c["verdict"] for c in conds if c.get("required")}
    cond_status = "衝突" if "false" in req_status else "未知" if "unknown" in req_status else \
        "符合" if req_status else "原文未附條件"
    lines: list[str] = []
    audit_lines: list[str] = []
    block = None
    if reason is None:
        block = {k: ([x for x in v if (x.get("unit_id"), k) not in dropped] if k in ITEM_KEYS else v)
                 for k, v in g["block"].items()}
        audit_lines = render(block, g["completeness"], [c for c in conds if c.get("required")], cond_status)
        # brief-v1：組有精簡呈現時，使用者只看一句結論＋中文做法（原文與逐項來源留在 audit_lines／參考依據）
        lines += render_brief(g["brief"], cond_status) if g.get("brief") else audit_lines
        if cond_status == "未知":
            unk = [q2 for c in conds if c["verdict"] == "unknown" for q2 in c.get("quotes") or []]
            q, q_note = f"請問您是否符合原文所說的情況：「{'；'.join(unk)}」？", "條件未知：改問該條件"
        elif g.get("brief") and q:
            q, q_note = None, "brief-v1：方法已完整列出，不顯示模型追問"
    else:
        lines.append(f"【結構化證據組裝】未提供方法：{reason}")
        if taken:
            lines.append(f"（引用這組證據的 {len(taken)} 條模型回答一併不顯示。）")
    for c in kept:
        lines.append(f"{_get(c, 'text')}{_src('、'.join(_get(c, 'unit_ids') or []))}")
    if kept and limitations:
        lines.append("限制：" + "；".join(limitations))
    if q:
        lines.append(f"想再確認：{q}")
    block_units = []
    if block:
        for k in ITEM_KEYS:
            block_units += [x["unit_id"] for x in block.get(k) or [] if x.get("unit_id")]
    cited = list(dict.fromkeys(block_units + [u for c in kept for u in _get(c, "unit_ids") or []]))
    status = ("complete" if g["completeness"].get("complete") else "incomplete") if block else "rejected"
    return {"version": VERSION, "status": status, "entry": entry, "relation_id": g.get("relation_id"),
            "reason": reason, "condition_status": cond_status, "conditions": conds, "completeness": g.get("completeness"),
            "lines": lines, "audit_lines": audit_lines, "brief": bool(block and g.get("brief")), "cited_units": cited, "kept_claims": [_get(c, "text") for c in kept],
            "replaced_claims": taken if block else [], "blocked_claims": [] if block else taken,
            "clarifying_question": q, "clarifying_question_note": q_note, "model_calls": 0}
