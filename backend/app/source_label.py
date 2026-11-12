"""引用出處的顯示文字（citation-label-v1，2026-11-01）。

使用者 2026-11-01：回答裡只看到文件編號（PR021、〔來源 1〕）沒有意義，應寫出
「論文／文章名稱、期刊／雜誌（或學校、網站）名稱、第幾頁」。本模組把這段文字集中在一處組出，
推薦卡文字（recommend）、問答回答本文（qa）、歷史結果的引用（results）都用同一種寫法：

    〈文章標題〉，刊物／學校／網站，年份，p.頁碼

資料來源與界線：
  - 標題、刊物、年份只取資料庫 documents.title 與 documents.bibliography（= config/sources.json 人工登錄、
    註明查證頁的書目）；刊物欄位用 bibliography.institution（期刊名＋卷期、學校系所、品牌雜誌或網站名）。
  - 書目沒有的欄位就不寫，不推測、不補（例如網頁文章多半沒有年份）。
  - 頁碼只來自片段的 page_start／page_end（PDF）；網頁文章沒有頁碼，依使用者 2026-10-25 的決定
    不顯示網頁段落定位（例如 body[1]>…），只寫到文章名稱與網站。
  - 文件編號（doc_key）與版本只留在日誌與內部欄位，不出現在給人看的出處文字。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, MutableMapping

import psycopg
from psycopg.rows import dict_row

VERSION = "citation-label-v1"
SEP = "，"   # 出處各欄位之間的分隔
MULTI_SEP = "；"   # 同一句引用多個來源時的分隔


# 由書目取出刊物／學校／網站名稱（bibliography.institution）；沒有或空白時回傳 None。
def venue_of(bibliography: Mapping[str, Any] | None) -> str | None:
    value = (bibliography or {}).get("institution")
    return value.strip() if isinstance(value, str) and value.strip() else None


# 由書目取出出版年份；沒有時回傳 None（不以擷取日期或其他日期代替）。
def year_of(bibliography: Mapping[str, Any] | None) -> int | str | None:
    value = (bibliography or {}).get("year")
    return value if value not in (None, "") else None


# 頁碼文字：單頁 p.4、跨頁 p.66–67；沒有頁碼（網頁文章）回傳 None。
def page_text(page_start: int | None, page_end: int | None) -> str | None:
    if page_start is None:
        return None
    return f"p.{page_start}" if page_end in (None, page_start) else f"p.{page_start}–{page_end}"


# 組出一筆來源的出處文字：〈標題〉，刊物，年份，p.頁碼；缺的欄位直接省略。
def label(title: str | None, venue: str | None = None, year: int | str | None = None,
          page_start: int | None = None, page_end: int | None = None) -> str:
    parts = [f"〈{title}〉" if title else "（未登錄標題的來源）", venue,
             str(year) if year is not None else None, page_text(page_start, page_end)]
    return SEP.join(p for p in parts if p)
# 核心邏輯：標題一律用〈〉框起來，讓使用者一眼分出「文章名稱」與後面的刊物／年份／頁碼；
# 標題理論上必有（documents.title NOT NULL），缺時寫明「未登錄標題」而不是改寫成文件編號。


# 依引用物件（需有 title、venue、year、page_start、page_end 欄位）組出出處文字。
def label_of(citation: Mapping[str, Any], *, with_page: bool = True) -> str:
    return label(citation.get("title"), citation.get("venue"), citation.get("year"),
                 citation.get("page_start") if with_page else None,
                 citation.get("page_end") if with_page else None)


# 一次查出多份文件的標題與書目；回傳 {doc_key: {"title", "bibliography"}}，查不到的文件不在結果裡。
def fetch_documents(conn: psycopg.Connection, doc_keys: Iterable[str]) -> dict[str, dict]:
    keys = list(dict.fromkeys(k for k in doc_keys if k))
    if not keys:
        return {}
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute("SELECT doc_key, title, bibliography FROM documents WHERE doc_key = ANY(%s)",
                           (keys,)).fetchall()
    return {r["doc_key"]: {"title": r["title"], "bibliography": r["bibliography"] or {}} for r in rows}


# 就地替一組引用物件補上 venue、year、source_label（出處文字）；標題以資料庫目前的 documents.title 為準。
def attach(conn: psycopg.Connection, citations: Iterable[MutableMapping[str, Any]]) -> None:
    cites = [c for c in citations if c.get("doc_key")]
    docs = fetch_documents(conn, (c["doc_key"] for c in cites))
    for c in cites:
        doc = docs.get(c["doc_key"])
        if doc is None:
            continue   # 文件已不存在：不補書目，維持原本欄位（之後的可用性檢查會把它標成無法查看）
        bib = doc["bibliography"]
        c.update(title=c.get("title") or doc["title"], venue=venue_of(bib), year=year_of(bib))
        c["source_label"] = label_of(c)
# 核心邏輯：
# 1. 一次查詢取回所有文件的書目，避免每個引用各查一次。
# 2. 只補「給人看」的欄位（venue、year、source_label），不刪不改 doc_key、chunk_key 等追溯用欄位（規範 3）。
