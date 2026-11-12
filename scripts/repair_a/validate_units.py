"""修復 A：支持單位檔驗證（契約欄位、原文對 rag_ready、子範圍、與前一版比對）。

用法：
    python scripts/repair_a/validate_units.py --root <資料根目錄> [--previous <前一版 support_units.jsonl>] --out <報告.json>

檢查：
- 每個單位有 contract-v1 support_unit 18 欄＋tokens_cl100k（非 None）；links 只有 prev/next/parent/corroborating_units
- 整段單位：unit_id＝chunk_id＝rag_ready block_id，text＝rag_ready display_text，text_sha256 相符
- #r 子範圍、#s 長段切片（F 版）：text＝母段 text[start:end]（連續子字串），chunk_id／links.parent＝母段
- #m 合併單位（F 版）：members 都是同一篇 rag_ready 中緊鄰的 block_id，text＝各 display_text 以兩個換行相接，
  unit_id＝main＋「#m」、chunk_id＝main
- conditions 每一項都是該單位 text 的子字串
- search_text 去掉〔原文術語〕段後＝normalize_search(text)（contract-v1.1）
- rag_ready 每個 block_id 都被整段單位、子範圍母段或合併成員涵蓋
- 給 --previous 時：前一版每個整段單位都還在（整段、子範圍母段或合併成員），留下的整段單位原文與前一版相同；
  search_text_sha256 新增的列為需要重編的 embedding 輸入（E-012）
任何一項不符，結束碼 1。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lipmatch import support_units as su  # noqa: E402

FIELDS = ["unit_id", "source_id", "source_version", "chunk_id", "text", "text_sha256", "search_text",
          "search_text_sha256", "locator_primary", "locator_secondary", "section_original", "section_search",
          "content_role", "support_scope", "conditions", "links", "product_mentions", "eligibility"]
LINK_KEYS = {"prev", "next", "parent", "corroborating_units"}


def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--previous", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    units = load(a.root / "data" / "processed" / "support_units" / "support_units.jsonl")
    rag, order = {}, {}
    for p in sorted((a.root / "data" / "processed" / "rag_ready").glob("*.jsonl")):
        for i, r in enumerate(load(p)):
            rag[r["block_id"]] = r
            order[r["block_id"]] = (p.stem, i)
    by_id = {u["unit_id"]: u for u in units}
    problems: list[str] = []
    for u in units:
        uid = u["unit_id"]
        missing = [f for f in FIELDS if f not in u]
        if missing:
            problems.append(f"{uid}：缺欄 {missing}")
        if not isinstance(u.get("tokens_cl100k"), int):
            problems.append(f"{uid}：tokens_cl100k 不是整數")
        if set(u.get("links") or {}) != LINK_KEYS:
            problems.append(f"{uid}：links 鍵 {sorted(u.get('links') or {})}")
        if u.get("text_sha256") != sha(u.get("text", "")):
            problems.append(f"{uid}：text_sha256 不符")
        if u.get("search_text_sha256") != sha(u.get("search_text", "")):
            problems.append(f"{uid}：search_text_sha256 不符")
        if su.search_base(u.get("search_text", "")) != su.normalize_search(u.get("text", "")):
            problems.append(f"{uid}：search_text 去掉〔原文術語〕段後不等於 normalize(text)（{su.SEARCH_NORMALIZE_VERSION}）")
        if u.get("eligibility") != "pending":
            problems.append(f"{uid}：eligibility 不是 pending")
        for c in u.get("conditions") or []:
            if c not in u["text"]:
                problems.append(f"{uid}：條件不是原文子字串：{c[:40]}")
        if "merge_range" in u:
            mr = u["merge_range"]
            members = mr.get("members") or []
            pos = [order.get(m) for m in members]
            if (uid != f"{mr.get('main')}#m" or u["chunk_id"] != mr.get("main") or mr.get("main") not in members
                    or None in pos or len({d for d, _ in pos}) != 1
                    or [i for _, i in pos] != list(range(pos[0][1], pos[0][1] + len(pos)))):
                problems.append(f"{uid}：合併成員不是同一篇緊鄰的 rag_ready 片段")
            elif "\n\n".join(rag[m]["display_text"] for m in members) != u["text"]:
                problems.append(f"{uid}：text 不等於成員 display_text 以兩個換行相接")
        elif "#r" in uid or "#s" in uid or "#a" in uid:
            # replace 模式沒有母段整段單位：改對 rag_ready 原文
            mother = by_id[u["chunk_id"]]["text"] if u["chunk_id"] in by_id else                 (rag.get(u["chunk_id"]) or {}).get("display_text")
            sr = u.get("sub_range") or {}
            if mother is None or u["links"]["parent"] != u["chunk_id"] or sr.get("of") != u["chunk_id"]:
                problems.append(f"{uid}：母段連結不符")
            elif mother[sr["start"]:sr["end"]] != u["text"]:
                problems.append(f"{uid}：不是母段連續子字串")
        else:
            r = rag.get(uid)
            if r is None or u["chunk_id"] != uid:
                problems.append(f"{uid}：rag_ready 沒有這個 block_id")
            elif r["display_text"] != u["text"]:
                problems.append(f"{uid}：text 與 rag_ready display_text 不同")
    parents = [u for u in units if not any(k in u["unit_id"] for k in ("#r", "#s", "#a")) and "merge_range" not in u]
    covered = ({u["unit_id"] for u in parents} | {u["chunk_id"] for u in units}
               | {m for u in units for m in (u.get("merge_range") or {}).get("members", [])})
    report = {"units": len(units), "parents": len(parents),
              "sub_ranges": sum("#r" in u["unit_id"] for u in units),
              "long_split_pieces": sum("#s" in u["unit_id"] for u in units),
              "approved_range_units": sum("#a" in u["unit_id"] for u in units),
              "merged": sum("merge_range" in u for u in units),
              "retrieval_excluded": sum(bool((u.get("retrieval") or {}).get("exclude")) for u in units),
              "rag_ready_records": len(rag),
              "parents_cover_rag_ready": covered == set(rag)}
    if not report["parents_cover_rag_ready"]:
        problems.append("整段單位集合與 rag_ready block_id 集合不同")
    if a.previous:
        prev_all = {u["unit_id"]: u for u in load(a.previous)}
        prev = {k: v for k, v in prev_all.items() if "#r" not in k}  # 整段單位對整段單位比；#r 另列
        same_ids = {u["unit_id"] for u in parents} <= set(prev) and set(prev) <= covered
        # embedding 快取以 search_text_sha256 為鍵（E-012）：前一版沒有的 SHA 才是新輸入
        prev_search = {u["search_text_sha256"] for u in prev_all.values()}
        cur_ids = {u["unit_id"] for u in units}
        report["search_text_changes"] = {
            "new_or_changed": [{"unit_id": u["unit_id"], "new_search_text_sha256": u["search_text_sha256"],
                                "old_search_text_sha256": (prev_all.get(u["unit_id"]) or {}).get("search_text_sha256"),
                                "tokens_cl100k": u.get("tokens_cl100k")}
                               for u in units if u["search_text_sha256"] not in prev_search],
            "removed_unit_ids": sorted(k for k in prev_all if k not in cur_ids),
        }
        text_same = sum(prev[u["unit_id"]]["text_sha256"] == u["text_sha256"] for u in parents if u["unit_id"] in prev)
        search_same = sum(prev[u["unit_id"]]["search_text_sha256"] == u["search_text_sha256"]
                          for u in parents if u["unit_id"] in prev)
        cond_changed = sum(prev[u["unit_id"]]["conditions"] != u["conditions"] for u in parents if u["unit_id"] in prev)
        prod_changed = sum(prev[u["unit_id"]]["product_mentions"] != u["product_mentions"]
                           for u in parents if u["unit_id"] in prev)
        report["vs_previous"] = {"previous_sha256": hashlib.sha256(a.previous.read_bytes()).hexdigest(),
                                 "same_parent_ids": same_ids, "text_same": text_same, "search_text_same": search_same,
                                 "conditions_changed": cond_changed, "product_mentions_changed": prod_changed,
                                 "new_embedding_inputs": len(report["search_text_changes"]["new_or_changed"])}
        if not same_ids or text_same != len(parents):
            problems.append("與前一版整段單位 ID 或原文不同")
    report["problems"] = problems
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "problems"}, ensure_ascii=False))
    print("OK" if not problems else "\n".join(problems[:30]))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
