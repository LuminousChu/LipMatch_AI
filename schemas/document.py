"""LipMatch AI — 第一階段文獻 ETL 的資料結構 (Pydantic v2)。

三個核心模型：
- SourceManifest：原始檔案的「身分證」(路徑、雜湊、處理頁碼…)，用來追溯來源。
- TextBlock：最小內容單位（標題／段落／表格／圖說…），同時保存 raw_text 與 cleaned_text。
- Document：一份文件 = manifest + blocks + issues（待人工確認的問題）。

設計原則：
- raw_text 一經擷取就不再修改；清洗結果只寫入 cleaned_text。
- 被判定為雜訊的區塊不刪除，而是標記 is_removed + removal_reason，保留稽核軌跡。
- 任何無法自動判斷的情況都寫成 ExtractionIssue，並把 needs_review 設為 True。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1.3.0"

SourceType = Literal["pdf", "html"]

BlockType = Literal[
    "heading",         # 有編號或大字級的章節標題，會更新 section 路徑
    "label",           # 內文字級的粗體短標籤（如 "Aim"、"Objective:"），不改變 section
    "paragraph",
    "list_item",
    "toc_entry",       # 目錄列（含 ..... 與頁碼）
    "table",           # 成功解析的表格，儲存格放在 table_data
    "table_caption",
    "figure_caption",
    "figure_text",     # 圖表（向量圖）內的軸標籤、數值等殘留文字，保留但需人工確認
    "formula",         # 獨立成行的公式（數學字型為主），保留原始字元與換行
    "equation_label",  # "Equation 2.1" 這類公式編號，透過 linked_block_ids 連到 formula
    "image_region",    # 點陣圖片區域（可能是圖片表格），沒有可擷取的文字
    "chart_region",    # 向量圖表區域，數據點無法結構化擷取
    "page_header",
    "page_footer",
]

Stage = Literal["extracted", "cleaned"]
Severity = Literal["info", "warning", "error"]
ParseStatus = Literal["ok", "partial", "failed"]
RegionKind = Literal["table", "table_image", "figure_image", "image", "chart", "formula"]
PageStatus = Literal["ok", "needs_review", "failed"]
BBox = tuple[float, float, float, float]


class TableStructure(BaseModel):
    """成功解析的表格之結構資訊。儲存格原文仍放在 TextBlock.table_data。

    column_names / row_labels / units 只取自表格本身的文字，不推測、不補值：
    找不到就是 None（或空 dict），並在 review_reasons 說明。
    """

    model_config = ConfigDict(extra="forbid")

    detection_method: Literal["pymupdf_lines", "pymupdf_words", "html"]
    n_rows: int = Field(..., ge=0)
    n_cols: int = Field(..., ge=0)
    header_rows: int = Field(default=0, ge=0)               # 表頭（欄名 + 單位列）佔幾列
    column_names: Optional[list[Optional[str]]] = None      # 欄名；None = 沒有辨識出表頭
    row_labels: Optional[list[Optional[str]]] = None        # 每個資料列的列名（第一欄文字）
    units: dict[str, str] = Field(default_factory=dict)     # {欄位索引(0-based 字串): 單位}
    caption: Optional[str] = None
    caption_block_id: Optional[str] = None
    cell_bboxes: Optional[list[list[Optional[BBox]]]] = None  # 與 table_data 同形狀
    text_coverage: Optional[float] = None                   # 表格範圍內文字有多少比例落入儲存格
    empty_cells: int = 0                                    # 資料區空白儲存格數（保留空值，不補）


class PageReport(BaseModel):
    """單頁的解析完整性指標（PDF 才有）。"""

    model_config = ConfigDict(extra="forbid")

    page_number: int = Field(..., ge=1)
    status: PageStatus
    text_chars: int                 # 頁面文字層的非空白字元數
    captured_chars: int             # 被某個 block 收錄的字元數（以字元多重集合比對）
    text_coverage: float
    blocks: int
    tables_parsed: int = 0
    tables_failed: int = 0          # 圖片表格、找不到表格的表格標題、低信心候選
    image_regions: int = 0
    chart_regions: int = 0
    formulas: int = 0
    formulas_need_review: int = 0
    reasons: list[str] = Field(default_factory=list)


class SourceManifest(BaseModel):
    """原始文件的來源資訊。只描述原始檔，不包含任何解析結果。"""

    model_config = ConfigDict(extra="forbid")

    doc_id: str = Field(..., pattern=r"^[A-Za-z0-9_\-]+$")
    source_path: str                      # 相對於專案根目錄的 POSIX 路徑
    source_type: SourceType
    sha256: str = Field(..., min_length=64, max_length=64)
    file_size_bytes: int = Field(..., ge=0)
    title: Optional[str] = None
    source_url: Optional[str] = None      # HTML 的 canonical URL（若有）
    total_pages: Optional[int] = None     # PDF 總頁數；HTML 為 None
    pages_processed: list[int] = Field(default_factory=list)  # 1-based 實體頁碼
    extractor: str                        # 例如 "pymupdf 1.26.x" / "beautifulsoup4 4.x"
    extracted_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    metadata: dict[str, str] = Field(default_factory=dict)


class ExtractionIssue(BaseModel):
    """需要人工確認的問題（無法辨識的表格、疑似遺失內容、無文字層頁面…）。"""

    model_config = ConfigDict(extra="forbid")

    issue_type: str                       # 機器可讀代碼，如 "table_rendered_as_image"
    severity: Severity = "warning"
    stage: Stage
    message: str
    page_number: Optional[int] = None
    block_id: Optional[str] = None
    details: dict[str, str] = Field(default_factory=dict)
    resolved: bool = False                # 人工確認後改為 True


class TextBlock(BaseModel):
    """文件中的一個內容區塊。"""

    model_config = ConfigDict(extra="forbid")

    doc_id: str
    block_id: str                         # 由 doc_id + 頁碼 + 順序組成，重跑時穩定不變
    block_type: BlockType
    order: int = Field(..., ge=0)         # 全文件閱讀順序
    section: Optional[str] = None         # 章節路徑，例如 "Chapter 2. Literature Review > 2.1 Colour Science"
    heading_level: Optional[int] = Field(default=None, ge=1, le=6)
    page_number: Optional[int] = Field(default=None, ge=1)  # PDF 實體頁碼 (1-based)；HTML 為 None
    bbox: Optional[tuple[float, float, float, float]] = None  # PDF 座標 (x0, y0, x1, y1)
    source_locator: Optional[str] = None  # HTML 的 DOM 路徑，或 PDF 的 "page:N"
    html_context: Optional[list[str]] = None  # HTML 祖先節點描述，例如 ["body", "nav#top.menu"]

    raw_text: str                         # 擷取當下的原文，之後永不修改
    cleaned_text: Optional[str] = None    # 只有 02_clean 會寫入
    search_text: Optional[str] = None     # 由 cleaned_text 衍生，只給搜尋／embedding 用（NFKC、原文黏字拆開）；引用原文請用 cleaned_text
    cited_works: list[str] = Field(default_factory=list)  # 段落中引用的他人研究，如 ["Zhang, 2019"]；用來區分本研究發現與引用
    table_data: Optional[list[list[Optional[str]]]] = None        # 原始儲存格
    cleaned_table_data: Optional[list[list[Optional[str]]]] = None

    table: Optional[TableStructure] = None  # block_type == "table" 時的結構資訊

    parse_status: ParseStatus = "ok"      # ok / partial（部分可疑）/ failed（區域內容無法擷取）
    failure_reason: Optional[str] = None  # partial / failed 時必填
    region_kind: Optional[RegionKind] = None
    linked_block_ids: list[str] = Field(default_factory=list)  # 標題↔表格/圖、公式↔編號、圖表↔軸標籤

    has_formula: bool = False             # 偵測到數學字型 / 數學符號
    is_removed: bool = False              # 清洗時判定為雜訊（內容仍保留在 raw_text）
    removal_reason: Optional[str] = None
    cleaning_ops: list[str] = Field(default_factory=list)  # 實際套用的清洗步驟
    continues_from_block_id: Optional[str] = None  # 跨頁接續的上一個區塊

    needs_review: bool = False
    review_reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_consistency(self) -> "TextBlock":
        if self.block_type == "table" and self.table_data is None:
            raise ValueError(f"{self.block_id}: table block 必須有 table_data")
        if self.block_type == "heading" and self.heading_level is None:
            raise ValueError(f"{self.block_id}: heading block 必須有 heading_level")
        if self.is_removed and not self.removal_reason:
            raise ValueError(f"{self.block_id}: is_removed=True 時必須說明 removal_reason")
        if self.review_reasons and not self.needs_review:
            raise ValueError(f"{self.block_id}: 有 review_reasons 時 needs_review 必須為 True")
        # 解析失敗的區域不得偽裝成「空白內容」
        if not self.raw_text.strip() and self.table_data is None and self.parse_status == "ok":
            raise ValueError(f"{self.block_id}: raw_text 為空時 parse_status 不可為 ok（應標記 failed 並說明原因）")
        if self.parse_status != "ok" and (not self.failure_reason or not self.needs_review):
            raise ValueError(f"{self.block_id}: parse_status={self.parse_status} 時必須有 failure_reason 且 needs_review=True")
        return self


class Document(BaseModel):
    """一份已擷取（或已清洗）的文件。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    stage: Stage
    manifest: SourceManifest
    blocks: list[TextBlock] = Field(default_factory=list)
    issues: list[ExtractionIssue] = Field(default_factory=list)
    pages: list[PageReport] = Field(default_factory=list)  # PDF 每頁完整性指標

    @model_validator(mode="after")
    def _check_blocks(self) -> "Document":
        seen: set[str] = set()
        for b in self.blocks:
            if b.doc_id != self.manifest.doc_id:
                raise ValueError(f"{b.block_id}: doc_id 與 manifest 不一致")
            if b.block_id in seen:
                raise ValueError(f"重複的 block_id: {b.block_id}")
            seen.add(b.block_id)
        if self.stage == "cleaned" and any(b.cleaned_text is None or b.search_text is None for b in self.blocks):
            raise ValueError("stage=cleaned 時每個 block 都必須有 cleaned_text 與 search_text")
        for b in self.blocks:
            for lid in b.linked_block_ids:
                if lid not in seen:
                    raise ValueError(f"{b.block_id}: linked_block_ids 指向不存在的 {lid}")
        for i in self.issues:
            if i.block_id and i.block_id not in seen:
                raise ValueError(f"issue {i.issue_type} 指向不存在的 block {i.block_id}")
        return self

    # ---- I/O ---------------------------------------------------------------

    def save(self, path: Path) -> Path:
        """以 UTF-8 JSON 原子寫入（先寫暫存檔再 rename），重複執行不會留下半寫入檔案。"""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, path)
        return path

    @classmethod
    def load(cls, path: Path) -> "Document":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))

    def review_queue(self) -> dict:
        """整理出需要人工確認的項目，輸出成獨立 JSON 方便審閱。"""
        return {
            "doc_id": self.manifest.doc_id,
            "stage": self.stage,
            "open_issues": [i.model_dump(mode="json") for i in self.issues if not i.resolved],
            "blocks_needing_review": [
                {
                    "block_id": b.block_id,
                    "block_type": b.block_type,
                    "page_number": b.page_number,
                    "section": b.section,
                    "reasons": b.review_reasons,
                    "parse_status": b.parse_status,
                    "failure_reason": b.failure_reason,
                    "region_kind": b.region_kind,
                    "bbox": b.bbox,
                    "raw_text_preview": b.raw_text[:300],
                }
                for b in self.blocks
                if b.needs_review
            ],
        }
