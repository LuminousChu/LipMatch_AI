// 來源瀏覽：只列出已人工核准、已發布、啟用中的片段，並依登入角色過濾（由後端決定）。
// 搜尋是關鍵字比對原文，不是語意檢索。

import { api, describeError } from "../api.js";
import { h, notice, replace } from "../dom.js";

interface SourceDoc {
  doc_key: string;
  title: string;
  version_label: string;
  visibility: string;
  chunks: number;
  url: string | null;
  year: string | null;
}

interface SourceChunk {
  chunk_key: string;
  doc_key: string;
  title: string;
  version_label: string;
  page_start: number | null;
  page_end: number | null;
  html_locator: string | null;
  section: string | null;
  quote_text: string;
  citation: string | null;
  cited_works: string[] | null;
  data_note: string | null;
}

const PAGE_SIZE = 20;

function pages(c: SourceChunk): string {
  if (c.page_start === null) return c.html_locator ?? "—";
  return c.page_end && c.page_end !== c.page_start ? `第 ${c.page_start}–${c.page_end} 頁` : `第 ${c.page_start} 頁`;
}

/** 只允許 http(s) 連結；其他協定（javascript: 等）一律不產生連結。 */
function safeLink(url: string | null, text: string): HTMLElement {
  if (url && /^https?:\/\//i.test(url)) {
    return h("a", { attrs: { href: url, target: "_blank", rel: "noopener noreferrer" } }, text);
  }
  return h("span", {}, text);
}

export async function renderSources(view: HTMLElement): Promise<void> {
  const docBox = h("div", { class: "chips" });
  const query = h("input", { attrs: { id: "src-q", type: "search", maxlength: "100", placeholder: "在原文中搜尋關鍵字" } });
  const results = h("div");
  let doc = "";
  let offset = 0;
  const form = h("form", { class: "search", on: { submit: (ev) => { ev.preventDefault(); offset = 0; void load(); } } },
    query, h("button", { attrs: { type: "submit" } }, "搜尋"));
  replace(view, h("h2", {}, "來源"),
    h("p", { class: "note" }, "這裡只會出現已發布的片段。搜尋是比對原文文字，不是語意搜尋。"),
    docBox, form, results);

  let docs: SourceDoc[] = [];
  try {
    docs = (await api<{ items: SourceDoc[] }>("GET", "/sources/documents")).items;
  } catch (err) {
    replace(results, notice("error", describeError(err)));
    return;
  }
  renderDocs();
  await load();

  function renderDocs(): void {
    const chip = (key: string, label: string) => h("button", {
      class: key === doc ? "chip active" : "chip", attrs: { type: "button" },
      on: { click: () => { doc = key; offset = 0; renderDocs(); void load(); } },
    }, label);
    replace(docBox, chip("", "全部文獻"), ...docs.map((d) => chip(d.doc_key, `${d.doc_key}（${d.chunks}）`)));
  }

  async function load(): Promise<void> {
    replace(results, h("p", { class: "note" }, "載入中…"));
    const params = new URLSearchParams({ offset: String(offset), limit: String(PAGE_SIZE) });
    if (doc) params.set("doc", doc);
    if (query.value.trim()) params.set("q", query.value.trim());
    try {
      const res = await api<{ total: number; items: SourceChunk[] }>("GET", `/sources/chunks?${params}`);
      if (res.total === 0) {
        replace(results, notice("info", docs.length === 0
          ? "目前沒有任何已核准並發布的片段。管理者完成審核並發布後，這裡才會有內容。"
          : "沒有符合的片段。"));
        return;
      }
      replace(results,
        h("p", { class: "note" }, `共 ${res.total} 筆，顯示第 ${offset + 1}–${offset + res.items.length} 筆`),
        ...res.items.map((c) => {
          const meta = docs.find((d) => d.doc_key === c.doc_key);
          return h("article", { class: "card chunk" },
            h("header", { class: "chunk-head" },
              h("strong", {}, c.title),
              h("span", { class: "conv-meta" }, `${c.doc_key} ${c.version_label} · ${pages(c)}${c.section ? ` · ${c.section}` : ""}`)),
            h("blockquote", { class: "quote" }, c.quote_text),
            // sources-cited-v1：後端已轉成陣列；仍防呆，不是陣列就不顯示這一行（避免整頁顯示錯誤）
            Array.isArray(c.cited_works) && c.cited_works.length > 0
              ? h("p", { class: "badge-line" }, h("span", { class: "badge warn" }, "含轉引"),
                  `這段引用其他研究：${c.cited_works.join("；")}，不是本研究的發現。`)
              : null,
            c.data_note ? h("p", { class: "badge-line" }, h("span", { class: "badge" }, "圖表"), c.data_note) : null,
            h("p", { class: "citation" }, c.citation ?? "", " ", meta?.url ? safeLink(meta.url, "原文連結") : null));
        }),
        h("div", { class: "pager" },
          h("button", { attrs: { type: "button", ...(offset === 0 ? { disabled: "" } : {}) },
            on: { click: () => { offset = Math.max(0, offset - PAGE_SIZE); void load(); } } }, "上一頁"),
          h("button", { attrs: { type: "button", ...(offset + PAGE_SIZE >= res.total ? { disabled: "" } : {}) },
            on: { click: () => { offset += PAGE_SIZE; void load(); } } }, "下一頁")));
    } catch (err) {
      replace(results, notice("error", describeError(err)));
    }
  }
}
