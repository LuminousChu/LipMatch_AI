"""修復 A：用本工作樹程式隔離重跑 01_extract → 02_clean → 05_export_rag_ready，輸出成可攜的資料根目錄。

用法：
    python scripts/repair_a/run_export.py --data-root F:/LipMatch_AI --out <輸出資料夾> [--docs TX001 PR015]

- 原始檔從 <data-root>/<documents.json 的 path> 讀；不寫入任何正式 data/interim、data/processed。
- 子程序設 LIPMATCH_DATA_ROOT=<data-root>，manifest.source_path 因此是相對路徑（D20），換資料夾仍可匯入。
- 輸出排成 backend ingest 認得的根目錄：<out>/config/（documents.json、sources.json 複本）、
  <out>/data/processed/cleaned、<out>/data/processed/rag_ready；另有 data/interim/extracted、reports、review、run_log.txt。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def run(cmd: list[str], log, env: dict) -> None:
    log.write("$ " + " ".join(cmd) + "\n")
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    log.write(r.stdout[-3000:] + r.stderr[-3000:] + "\n")
    if r.returncode != 0:
        raise SystemExit(f"失敗：{' '.join(cmd)}\n{r.stderr[-2000:]}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="隔離重跑 ETL 並輸出可攜資料根目錄")
    ap.add_argument("--data-root", type=Path, required=True, help="原始檔所在的專案資料根目錄（含 data/raw）")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--docs", nargs="*")
    ap.add_argument("--python", default=sys.executable)
    a = ap.parse_args(argv)
    data_root = a.data_root.resolve()
    out = a.out.resolve()
    cfg = json.loads((ROOT / "config" / "documents.json").read_text(encoding="utf-8"))["documents"]
    docs = [d for d in cfg if not a.docs or d["doc_id"] in a.docs]
    dirs = {k: out / v for k, v in {"extracted": "data/interim/extracted", "cleaned": "data/processed/cleaned",
                                     "rag_ready": "data/processed/rag_ready", "reports": "reports",
                                     "review": "review"}.items()}
    for p in dirs.values():
        p.mkdir(parents=True, exist_ok=True)
    (out / "config").mkdir(parents=True, exist_ok=True)
    for name in ("documents.json", "sources.json"):
        shutil.copyfile(ROOT / "config" / name, out / "config" / name)
    env = {**os.environ, "LIPMATCH_DATA_ROOT": str(data_root), "PYTHONUTF8": "1"}
    py = a.python
    with open(out / "run_log.txt", "a", encoding="utf-8") as log:
        log.write(f"code={ROOT} data_root={data_root} docs={[d['doc_id'] for d in docs]} start={time.ctime()}\n")
        for d in docs:
            did = d["doc_id"]
            src = data_root / d["path"]
            if not src.is_file():
                log.write(f"略過 {did}：找不到 {src}\n")
                print(did, "missing", flush=True)
                continue
            ex = [py, str(ROOT / "scripts" / "01_extract.py"), str(src), "--doc-id", did,
                  "--out", str(dirs["extracted"]), "--review-dir", str(dirs["review"])]
            if d.get("pages"):
                ex += ["--pages", d["pages"]]
            run(ex, log, env)
            run([py, str(ROOT / "scripts" / "02_clean.py"), str(dirs["extracted"] / f"{did}.json"),
                 "--out", str(dirs["cleaned"]), "--report-dir", str(dirs["reports"]),
                 "--review-dir", str(dirs["review"])], log, env)
            cleaned = json.loads((dirs["cleaned"] / f"{did}.json").read_text(encoding="utf-8"))
            unchecked = [i["message"] for i in cleaned["issues"] if i["issue_type"] == "section_scope_unchecked"]
            if unchecked:  # 章節範圍修復（cd1a1b3）沒生效就停，不產出看似正常的舊行為匯出
                raise SystemExit(f"{did}：{unchecked[0]}")
            run([py, str(ROOT / "scripts" / "05_export_rag_ready.py"), str(dirs["cleaned"] / f"{did}.json"),
                 "--out", str(dirs["rag_ready"]), "--sources", str(ROOT / "config" / "sources.json")], log, env)
            print(did, "ok", flush=True)
        log.write(f"end={time.ctime()}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
