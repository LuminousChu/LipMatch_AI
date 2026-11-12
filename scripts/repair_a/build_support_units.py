"""修復 A：由隔離匯出的資料根目錄產生來源支持單位 JSONL 與可攜 manifest。

用法：
    python scripts/repair_a/build_support_units.py --root <run_export.py 的輸出> [--docs TX001 PR015] [--no-split]
    python scripts/repair_a/build_support_units.py --verify <root>/manifest.json

輸出（都在 --root 底下，manifest 只記相對路徑）：
    data/processed/support_units/support_units.jsonl   每行一個單位（lipmatch/support_units.py）
    data/processed/support_units/manifest.json         D 格式（D-REQ-05）：files[path,sha256,lines]、generator_commit、
                                                       input_export_sha256
    manifest.json   config／cleaned／rag_ready／support_units 每個檔案的相對路徑、SHA-256、行數；產生程式 commit

tokens_cl100k：用 B／G1 計 2,048 預算時同一份本機 tiktoken 0.14.0 與 cl100k_base 快取（不下載）；
位置可用 LIPMATCH_TIKTOKEN_DEPS、LIPMATCH_TIKTOKEN_CACHE 覆寫。載入不到就停，不用估算值頂替。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lipmatch import support_units as su  # noqa: E402

_TOK_BASE = Path.home() / ".lipmatch" / "tiktoken"
CL100K_SHA256 = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"  # cl100k_base.tiktoken 公開檔


def cl100k_counter() -> tuple[callable, dict]:
    deps = Path(os.environ.get("LIPMATCH_TIKTOKEN_DEPS", _TOK_BASE / "token-counter-deps"))
    cache = Path(os.environ.get("LIPMATCH_TIKTOKEN_CACHE", _TOK_BASE / "token-counter-cache"))
    cached = [p for p in cache.glob("*") if p.is_file()]
    if not cached or not any(su.sha256_file(p) == CL100K_SHA256 for p in cached):
        raise SystemExit(f"找不到 SHA 相符的 cl100k_base 快取（{cache}）；不下載、不估算")
    sys.path.insert(0, str(deps))
    os.environ["TIKTOKEN_CACHE_DIR"] = str(cache)
    import tiktoken
    enc = tiktoken.get_encoding("cl100k_base")
    return (lambda t: len(enc.encode(t, disallowed_special=()))), {
        "tokenizer": f"tiktoken {tiktoken.__version__} cl100k_base", "encoding_file_sha256": CL100K_SHA256}


def code_version() -> dict:
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True).stdout.strip()
    return {"commit": git("rev-parse", "HEAD"), "branch": git("branch", "--show-current"),
            "dirty_files": [line[3:] for line in git("status", "--porcelain").splitlines()]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="產生來源支持單位與可攜 manifest")
    ap.add_argument("--root", type=Path)
    ap.add_argument("--docs", nargs="*")
    ap.add_argument("--no-split", action="store_true", help="不產生 #r 多情境子範圍")
    ap.add_argument("--split-mode", choices=["both", "replace"], default="both",
                    help="both：母段整段單位＋子範圍都輸出；replace：拆分段只輸出子範圍（D aad1dab 之後的 _apply_units 格式）")
    ap.add_argument("--f-rules", action="store_true",
                    help="切片整修 F 版：長段切（#s）、參考文獻不進檢索、碎片併入（#m）；見 lipmatch/support_units.py")
    ap.add_argument("--overlap-10-20", action="store_true",
                    help="搭配 --f-rules：長段切的相鄰子片重疊前一片的 10～20%%（取代 ≤64 token 句界重疊）")
    ap.add_argument("--verify", type=Path, help="只驗證 manifest（可在另一個資料夾解包後執行）")
    a = ap.parse_args(argv)
    if a.verify:
        problems = su.verify_manifest(a.verify)
        print("manifest OK" if not problems else "\n".join(problems))
        return 1 if problems else 0
    root = a.root.resolve()
    docs = json.loads((root / "config" / "documents.json").read_text(encoding="utf-8"))["documents"]
    sources = {k: v for k, v in json.loads((root / "config" / "sources.json").read_text(encoding="utf-8")).items()
               if not k.startswith("_")}
    ids = [d["doc_id"] for d in docs if (not a.docs or d["doc_id"] in a.docs)
           and (root / "data" / "processed" / "rag_ready" / f"{d['doc_id']}.jsonl").is_file()]
    count, tok_info = cl100k_counter()
    version = code_version()
    out_dir = root / "data" / "processed" / "support_units"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "support_units.jsonl"
    n = subs = 0
    f_counts = {"long_split_units": 0, "merged_units": 0, "retrieval_excluded_units": 0}
    export_shas: dict[str, str] = {}
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        for doc_id in ids:
            doc = su.load_doc(root, doc_id, sources)
            export_shas[doc_id] = doc.export_sha256
            for u in su.build_units(doc, count, split=not a.no_split, keep_split_parent=a.split_mode == "both",
                                    long_split=a.f_rules, exclude_references=a.f_rules, merge_fragments=a.f_rules,
                                    long_split_overlap=a.f_rules and a.overlap_10_20):
                f.write(json.dumps(u, ensure_ascii=False) + "\n")
                n += 1
                subs += "#r" in u["unit_id"]
                f_counts["long_split_units"] += "#s" in u["unit_id"]
                f_counts["merged_units"] += "merge_range" in u
                f_counts["retrieval_excluded_units"] += bool(u.get("retrieval", {}).get("exclude"))
    # 輸入匯出指紋：各文件 rag_ready jsonl SHA-256 依 doc_id 排序後的正規 JSON 再取 SHA-256
    input_export_sha256 = su.sha256_text(json.dumps(dict(sorted(export_shas.items())), ensure_ascii=False,
                                                    sort_keys=True, separators=(",", ":")))
    d_manifest = su.build_manifest(out_dir, [out], {
        "schema": "support-unit-manifest-d-v1",
        "generator_commit": version["commit"],
        "generator_dirty_files": version["dirty_files"],
        "input_export_sha256": input_export_sha256,
        "input_export_sha256_rule": "sha256(canonical JSON {doc_id: rag_ready jsonl sha256})",
        "input_exports": dict(sorted(export_shas.items())),
        "units": n, "sub_range_units": subs, "split_mode": a.split_mode, "rules_version": su.RULES_VERSION,
        "search_normalize_version": su.SEARCH_NORMALIZE_VERSION, **tok_info,
        **({"f_rules": {"long_split": su.LONG_SPLIT_OVERLAP_VERSION if a.overlap_10_20 else su.LONG_SPLIT_VERSION,
                        "long_overlap": [su.LONG_OVERLAP_MIN, su.LONG_OVERLAP_MAX] if a.overlap_10_20 else None,
                        "reference": su.REFERENCE_RULE,
                        "merge": su.MERGE_VERSION, "long_split_over": su.LONG_SPLIT_OVER,
                        "long_piece_max": su.LONG_PIECE_MAX, "long_piece_overlap": su.LONG_PIECE_OVERLAP,
                        "merge_max": su.MERGE_MAX, **f_counts}} if a.f_rules else {}),
    })
    (out_dir / "manifest.json").write_text(json.dumps(d_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    files = [root / "config" / "documents.json", root / "config" / "sources.json", out, out_dir / "manifest.json"]
    for doc_id in ids:
        files += [root / "data" / "processed" / "rag_ready" / f"{doc_id}.jsonl",
                  root / "data" / "processed" / "cleaned" / f"{doc_id}.json"]
    manifest = su.build_manifest(root, files, {
        "schema": "support-unit-manifest-v0",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generator": {"script": "scripts/repair_a/build_support_units.py", "module": "lipmatch/support_units.py",
                      "unit_schema": su.SCHEMA, "rules_version": su.RULES_VERSION,
                      "lexicon_sha256": su.LEXICON_SHA256, "search_normalize_version": su.SEARCH_NORMALIZE_VERSION,
                      **version, **tok_info},
        "docs": ids,
        "units": n,
        "sub_range_units": subs,
        "input_export_sha256": input_export_sha256,
        "units_file": out.relative_to(root).as_posix(),
        "paths_are_relative_to": "manifest.json 所在資料夾",
        "note": "eligibility 一律 pending：ETL 匯出與規則標記都不是人工核准；content_role／support_scope／conditions 為規則結果",
    })
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{n} units（含 #r 子範圍 {subs}）from {len(ids)} docs → {out}；manifest → {root / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
