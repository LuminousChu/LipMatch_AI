# 文獻 ETL Pipeline

把學術 PDF 與彩妝 HTML 文章解析成結構化 JSON，並做資料清洗與解析完整性檢查。
ETL 本身不含 Chunking、Embedding、LLM 或 OCR。

## 環境

Python 3.11+

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
```

## 執行

一行指令跑完所有文獻（01 擷取 → 02 清洗 → 03 完整性報告 → 05 RAG 匯出，Leeds 另跑 04 表格檢查）：

```bash
.venv/Scripts/python scripts/run_all.py
```

- `--list`：列出登錄的文獻、檔案是否在這台電腦、是否有書目，不執行。
- `--only TH014 AR015`：只處理指定的文獻。
- 檔案不在這台電腦上的文獻會被略過；某篇出錯不影響其他篇，最後印出總表。

### 新增一篇文獻

1. 把 PDF / HTML 放進 `data/raw/`（可放子資料夾，例如 `data/raw/academic_papers/`；此資料夾不進 git）。
2. 在 `config/documents.json` 加一筆 `{"doc_id": "...", "path": "data/raw/..."}`。
   doc_id 只能用英數、底線、連字號（依文獻主清冊編碼，例如 `TH014`）。
3. 在 `config/sources.json` 登錄書目（作者、年份、標題、查證頁）；沒登錄的話 01–03 照跑，但不會匯出 RAG。
4. 執行 `run_all.py`。`data/raw/` 有檔案還沒登錄時，總表最後會提醒。

### 單獨執行某一步

```bash
.venv/Scripts/python scripts/01_extract.py data/raw/academic_papers/TH013_唇彩影響臉部色彩感知_英文文獻_2024.pdf --doc-id TH013
.venv/Scripts/python scripts/02_clean.py data/interim/extracted/
.venv/Scripts/python scripts/03_integrity_report.py data/processed/cleaned/
.venv/Scripts/python scripts/05_export_rag_ready.py data/processed/cleaned/
```

| 步驟 | 輸出 |
|---|---|
| 01_extract | `data/interim/extracted/<doc_id>.json`、`data/review/<doc_id>.extract_review.json` |
| 02_clean | `data/processed/cleaned/<doc_id>.json`、`data/processed/reports/<doc_id>.clean_report.md` |
| 03_integrity_report | `data/review/<doc_id>.integrity_report.{json,md}` |
| 04_check_tables | `data/review/tables/TH013_table_check.md`（人工轉錄表格的交叉檢查） |
| 05_export_rag_ready | `data/processed/rag_ready/<doc_id>.jsonl`（可進 RAG 的段落 + 出處）、`<doc_id>.summary.md` |

新增文獻時，必須先在 `config/sources.json` 登錄書目資訊（作者、年份、標題、查證頁），
否則 05 會拒絕匯出：沒有出處的段落不進 RAG。篩選規則只定義在 `lipmatch/eligibility.py`。

## 測試

```bash
.venv/Scripts/python -m pytest -v
```

需要 TH013 原檔（`data/raw/academic_papers/TH013_*.pdf`）的測試在檔案不存在時會自動略過；
合成 PDF 與 HTML 測試資料位於 `tests/fixtures/`（SYNTHETIC，非真實數據）。
