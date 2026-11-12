"""文獻出處：讀取 config/sources.json，產生 RAG 回答用的引用字串。

PDF 內建 metadata 常是空的（Leeds 論文的 title/author 都是空字串），
所以書目資訊改由人工登錄在 config/sources.json，並註明查證頁碼。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCES = ROOT / "config" / "sources.json"


class SourceInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    authors: list[str]
    author_citation: str                  # 引用格式的作者名，例如 "Chen, Y."（人工填寫，不自動拆姓名）
    year: Optional[int] = None
    doc_type: Optional[str] = None        # 例如 "Master by Research thesis"、"journal article"、"web article"
    institution: Optional[str] = None
    url: Optional[str] = None
    related_publication: Optional[str] = None
    verified_from: str                    # 書目資訊在原文的哪一頁查到
    # 以下為網路來源（PR032～PR035，ingest-pongo 分支）補充的追溯欄位，選填
    date: Optional[str] = None            # 網頁發文日期（未核實就不填）
    license_status: Optional[str] = None  # 授權狀態，例如 "pending_review"
    evidence_scope: Optional[str] = None  # 這份來源只能支持什麼範圍的說法
    references: Optional[list[dict]] = None  # 原文轉引的研究（逐段對應），只作追溯，不當證據


def load_sources(path: Path = DEFAULT_SOURCES) -> dict[str, SourceInfo]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {k: SourceInfo.model_validate(v) for k, v in data.items() if not k.startswith("_")}


def reference_string(src: SourceInfo) -> str:
    """APA 風格的文獻字串：Chen, Y. (2024). Title [Master by Research thesis]. University of Leeds, School of Design."""
    parts = [f"{src.author_citation} ({src.year if src.year else 'n.d.'}). {src.title}"]
    if src.doc_type:
        parts[0] += f" [{src.doc_type}]"
    parts[0] += "."
    if src.institution:
        parts.append(f"{src.institution}.")
    if src.url:
        parts.append(src.url)
    return " ".join(parts)


def short_citation(src: SourceInfo, pages: list[int]) -> str:
    """文中引用：(Chen, 2024, p.20) / (Chen, 2024, pp.12–13)。HTML 沒有頁碼時省略。"""
    surname = src.author_citation.split(",")[0].strip()
    year = src.year if src.year else "n.d."
    pages = sorted(set(p for p in pages if p is not None))
    if not pages:
        return f"({surname}, {year})"
    loc = f"p.{pages[0]}" if len(pages) == 1 else f"pp.{pages[0]}–{pages[-1]}"
    return f"({surname}, {year}, {loc})"
