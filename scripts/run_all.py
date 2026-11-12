"""run_all.py — 一行指令跑完所有文獻的 ETL：01 擷取 → 02 清洗 → 03 完整性報告 → 05 RAG 匯出（→ 04 表格檢查）。

用法：
    python scripts/run_all.py                      # 處理 config/documents.json 裡的全部文獻
    python scripts/run_all.py --only TH014         # 只處理指定的 doc_id（可多個）
    python scripts/run_all.py --list               # 列出登錄的文獻與檔案是否存在，不執行

行為：
- 文獻清單在 config/documents.json（doc_id、檔案路徑、頁碼範圍、是否做表格檢查）。
- 檔案不在這台電腦上 → 略過並提醒，其他文獻照跑（換電腦時 PDF 不一定都帶過去）。
- 某篇出錯 → 記下錯誤，繼續處理下一篇；最後的總表會列出失敗原因，結束碼為 1。
- 沒登錄書目（config/sources.json）→ 01–03 照跑，05 RAG 匯出略過並提醒。
- data/raw/ 裡有檔案沒登錄 → 提醒要加進 config/documents.json。
- 各步驟直接呼叫原本的 scripts/0X_*.py，輸出位置與單獨執行時相同。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pydantic import BaseModel, ConfigDict, Field  # noqa: E402

DEFAULT_CONFIG = ROOT / "config" / "documents.json"
DEFAULT_SOURCES = ROOT / "config" / "sources.json"
RAW_DIR = ROOT / "data" / "raw"
SUPPORTED = {".pdf", ".html", ".htm"}


class DocumentEntry(BaseModel):
    """config/documents.json 裡的一筆文獻。"""

    model_config = ConfigDict(extra="forbid")

    doc_id: str = Field(..., pattern=r"^[A-Za-z0-9_\-]+$")
    path: str                              # 相對於專案根目錄
    pages: Optional[str] = None            # 例如 "1-12"；None = 全部頁
    table_check: bool = False              # 是否跑 04_check_tables.py

    @property
    def abs_path(self) -> Path:
        return (ROOT / self.path).resolve()


def load_config(path: Path) -> list[DocumentEntry]:
    """讀取並驗證文獻清單：doc_id 與路徑都不可重複。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = [DocumentEntry.model_validate(d) for d in data.get("documents", [])]
    for attr in ("doc_id", "path"):
        seen: set[str] = set()
        for e in entries:
            key = getattr(e, attr)
            if key in seen:
                raise ValueError(f"{path}：重複的 {attr}「{key}」")
            seen.add(key)
    return entries


def load_script(filename: str):
    """載入檔名以數字開頭的 scripts/0X_*.py（不能用一般 import）。"""
    name = "lipmatch_step_" + filename.split("_", 1)[0]
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def unregistered_files(entries: list[DocumentEntry], raw_dir: Path = RAW_DIR) -> list[Path]:
    """data/raw/ 底下（含子資料夾）還沒登錄的 PDF / HTML。"""
    registered = {e.abs_path for e in entries}
    return sorted(p for p in raw_dir.rglob("*") if p.suffix.lower() in SUPPORTED and p.resolve() not in registered)


def registered_sources(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {k for k in json.loads(path.read_text(encoding="utf-8")) if not k.startswith("_")}


def process(entry: DocumentEntry, dirs: dict[str, Path], sources_path: Path, has_source: bool) -> list[str]:
    """對一篇文獻依序執行 01 → 02 → 03 →（05）。回傳已完成的步驟名稱；出錯直接拋出例外。"""
    extract, clean = load_script("01_extract.py"), load_script("02_clean.py")
    report, rag = load_script("03_integrity_report.py"), load_script("05_export_rag_ready.py")
    done: list[str] = []

    args = [str(entry.abs_path), "--doc-id", entry.doc_id, "--out", str(dirs["extracted"]),
            "--review-dir", str(dirs["review"])]
    if entry.pages:
        args += ["--pages", entry.pages]
    extract.main(args)
    done.append("01")

    extracted = dirs["extracted"] / f"{entry.doc_id}.json"
    clean.main([str(extracted), "--out", str(dirs["cleaned"]), "--report-dir", str(dirs["reports"]),
                "--review-dir", str(dirs["review"])])
    done.append("02")

    cleaned = dirs["cleaned"] / f"{entry.doc_id}.json"
    report.main([str(cleaned), "--out", str(dirs["review"])])
    done.append("03")

    if has_source:
        rag.main([str(cleaned), "--out", str(dirs["rag_ready"]), "--sources", str(sources_path)])
        done.append("05")
    return done


def default_dirs(base: Optional[Path] = None) -> dict[str, Path]:
    """各步驟的輸出資料夾。base=None 時與各 script 單獨執行的預設位置相同。"""
    data = (base or ROOT / "data")
    return {
        "extracted": data / "interim" / "extracted",
        "cleaned": data / "processed" / "cleaned",
        "reports": data / "processed" / "reports",
        "rag_ready": data / "processed" / "rag_ready",
        "review": data / "review",
    }


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="LipMatch AI：一次跑完所有文獻的 ETL（01→02→03→05）")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="文獻清單（預設 config/documents.json）")
    ap.add_argument("--sources", type=Path, default=DEFAULT_SOURCES, help="書目資訊（預設 config/sources.json）")
    ap.add_argument("--only", nargs="+", metavar="DOC_ID", help="只處理這些 doc_id")
    ap.add_argument("--list", action="store_true", help="只列出登錄的文獻，不執行")
    ap.add_argument("--data-dir", type=Path, help="輸出根目錄（預設 data/；測試用）")
    args = ap.parse_args(argv)

    entries = load_config(args.config)
    if args.only:
        unknown = sorted(set(args.only) - {e.doc_id for e in entries})
        if unknown:
            print(f"[run_all] 錯誤：{unknown} 不在 {args.config}")
            return 2
        entries = [e for e in entries if e.doc_id in args.only]
    sources = registered_sources(args.sources)

    if args.list:
        for e in entries:
            exists = "✓" if e.abs_path.exists() else "✗ 找不到檔案"
            src = "書目 ✓" if e.doc_id in sources else "書目 ✗（05 會略過）"
            print(f"  {e.doc_id:<16} {exists:<10} {src:<16} {e.path}")
        return 0

    dirs = default_dirs(args.data_dir)
    results: list[tuple[str, str, str]] = []   # (doc_id, 狀態, 說明)
    t0 = time.time()
    for e in entries:
        print(f"\n===== {e.doc_id} =====")
        if not e.abs_path.exists():
            print(f"[run_all] 略過：找不到 {e.path}（這台電腦沒有這個檔案？）")
            results.append((e.doc_id, "略過", "找不到檔案"))
            continue
        has_source = e.doc_id in sources
        if not has_source:
            print(f"[run_all] 提醒：{e.doc_id} 尚未登錄於 {args.sources.name}，05 RAG 匯出將略過")
        try:
            done = process(e, dirs, args.sources, has_source)
            note = "→".join(done) + ("" if has_source else "（缺書目，未匯出 RAG）")
            results.append((e.doc_id, "完成", note))
        except Exception as exc:  # 一篇失敗不影響其他篇
            traceback.print_exc()
            results.append((e.doc_id, "失敗", f"{type(exc).__name__}: {exc}"[:200]))

    if any(e.table_check and e.abs_path.exists() for e in entries) and (ROOT / "data" / "review" / "tables").exists():
        print("\n===== 04 表格交叉檢查 =====")
        try:
            load_script("04_check_tables.py").main([])
            results.append(("04_check_tables", "完成", "data/review/tables/"))
        except Exception as exc:
            traceback.print_exc()
            results.append(("04_check_tables", "失敗", f"{type(exc).__name__}: {exc}"[:200]))

    print(f"\n===== 總結（{time.time() - t0:.1f} 秒）=====")
    for doc_id, status, note in results:
        print(f"  {status}  {doc_id:<16} {note}")
    if args.data_dir is None:
        missing = unregistered_files(load_config(args.config))
        if missing:
            print("\n[run_all] data/raw/ 裡有檔案還沒登錄（要處理的話加進 config/documents.json）：")
            for p in missing:
                print(f"  - {p.relative_to(ROOT).as_posix()}")
    return 1 if any(status == "失敗" for _, status, _ in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
