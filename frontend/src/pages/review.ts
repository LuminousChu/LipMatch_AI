// 管理者審核頁：逐筆對照原文、填理由後核准或退回。刻意不提供批次核准（規範 4）。
// 審核人一律是目前登入的管理者（由後端 session 決定）。
// 版本提示（2026-10-18）：顯示文件、片段所屬版本、版本狀態與是否最新，「最新草稿」與「目前發布版本」分開；
// 核准前跳出確認框列出將核准的文件／版本／片段，送出時把這三項原樣帶給後端比對（不一致後端回 409）。
// 本頁不會自動轉移核准，也不改任何既有核准狀態；每一筆仍由審核人自己決定。

import { api, describeError } from "../api.js";
import { formatTime, h, notice, replace } from "../dom.js";

interface VersionRow {
  doc_key: string;
  visibility: "all" | "admin";
  is_active: boolean;
  version_label: string;
  status: "draft" | "published" | "retired";
  chunks: number;
  pending: number;
  approved: number;
  rejected: number;
  is_latest: boolean;
  latest_label: string;
  latest_draft_label: string | null;
  published_label: string | null;
}

/** 審核清單所屬版本的資訊（後端 /admin/review/chunks 回應的 version）。 */
interface VersionInfo {
  doc_key: string;
  version_label: string;
  status: "draft" | "published" | "retired";
  is_active: boolean;
  is_latest: boolean;
  latest_label: string;
  latest_draft_label: string | null;
  published_label: string | null;
}

type ReviewStatus = "pending" | "approved" | "rejected";

interface ReviewItem {
  id: number;
  doc_key: string;
  chunk_key: string;
  version_label: string;
  version_status: string;
  pages: string | null;
  html_locator: string | null;
  section: string | null;
  review_status: ReviewStatus;
  prior_decision: string | null;
  content_sha256: string;
  quote_text: string;
  flags: string[];
  reviewed_by: string | null;
  reviewed_at: string | null;
  review_note: string | null;
}

interface ReviewPage {
  total: number;
  items: ReviewItem[];
  flag_labels: Record<string, string>;
  version: VersionInfo;
}

interface ReviewEvent {
  from_status: ReviewStatus;
  to_status: ReviewStatus;
  reason: string;
  created_at: string;
  reviewer: string;
}

const PAGE_SIZE = 20;
const STATUS_LABEL: Record<ReviewStatus, string> = { pending: "待審", approved: "已核准", rejected: "已退回" };
const VERSION_LABEL: Record<string, string> = { draft: "草稿", published: "已發布", retired: "已退役" };

export async function renderReview(view: HTMLElement): Promise<void> {
  const versionsBox = h("div");
  const listBox = h("div");
  const pageMsg = h("div");
  replace(view, h("h2", {}, "片段審核"),
    notice("info", "每一筆都要對照原始文獻的頁碼（網頁為段落定位）確認內容，填寫理由後才能核准或退回；系統不提供批次核准。"),
    pageMsg, versionsBox, listBox);

  let selected: { doc: string; version: string } | null = null;
  let status: ReviewStatus = "pending";
  let offset = 0;
  await loadVersions();

  async function loadVersions(): Promise<void> {
    let rows: VersionRow[];
    try {
      rows = (await api<{ items: VersionRow[] }>("GET", "/admin/review/documents")).items;
    } catch (err) {
      replace(versionsBox, notice("error", describeError(err)));
      return;
    }
    if (rows.length === 0) {
      replace(versionsBox, notice("info", "資料庫裡還沒有匯入任何文獻（請先執行 python -m app.ingest import）。"));
      return;
    }
    if (selected === null) {
      // 停用的文件（例如改編號前的 LEEDS_2024）與非最新版本不預設選取，避免審到舊編號或舊版本
      const first = rows.find((r) => r.is_active && r.is_latest && r.status !== "retired") ?? rows[0];
      if (first) selected = { doc: first.doc_key, version: first.version_label };
    }
    replace(versionsBox, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, ...["文獻", "版本", "狀態", "待審", "核准", "退回", "可見範圍", ""].map((t) => h("th", {}, t)))),
      h("tbody", {}, ...rows.map((r) => {
        const isSel = selected?.doc === r.doc_key && selected.version === r.version_label;
        return h("tr", { class: isSel ? "selected" : "" },
          h("td", {}, r.doc_key, r.is_active ? null : h("span", { class: "note" }, "（已停用）")), h("td", {}, r.version_label, " ", latestBadge(r)), h("td", {}, VERSION_LABEL[r.status] ?? r.status),
          h("td", {}, r.pending), h("td", {}, r.approved), h("td", {}, r.rejected),
          h("td", {}, r.visibility === "all" ? "所有使用者" : "僅管理者",
            " ", h("button", { class: "link", attrs: { type: "button" }, on: { click: () => void toggleVisibility(r) } },
              r.visibility === "all" ? "改為僅管理者" : "開放給所有使用者")),
          h("td", {},
            h("button", { class: "link", attrs: { type: "button" },
              on: { click: () => { selected = { doc: r.doc_key, version: r.version_label }; offset = 0; void loadVersions(); } } }, "審核"),
            r.status === "draft" ? h("button", { class: "link", data: { publish: "1" }, attrs: { type: "button", ...(r.approved === 0 ? { disabled: "" } : {}) },
              on: { click: () => void publish(r) } }, "發布") : null));
      }))));
    await loadChunks();
  }

  async function toggleVisibility(r: VersionRow): Promise<void> {
    const next = r.visibility === "all" ? "admin" : "all";
    if (next === "all" && !confirm(`確定讓所有使用者都能看到 ${r.doc_key} 已發布的核准片段？`)) return;
    try {
      await api("PUT", `/admin/documents/${encodeURIComponent(r.doc_key)}/visibility`, { visibility: next });
      replace(pageMsg, notice("ok", `${r.doc_key} 已改為${next === "all" ? "所有使用者可見" : "僅管理者可見"}。`));
      await loadVersions();
    } catch (err) {
      replace(pageMsg, notice("error", describeError(err)));
    }
  }

  async function publish(r: VersionRow): Promise<void> {
    if (!confirm(`發布 ${r.doc_key} ${r.version_label}？\n已核准 ${r.approved} 筆會變成可檢索；待審 ${r.pending} 筆之後仍可繼續審核。\n若已有發布中的版本，會被改成退役。`)) return;
    try {
      const res = await api<{ published: string; retired: string | null }>("POST", "/admin/review/publish",
        { doc_key: r.doc_key, version_label: r.version_label });
      replace(pageMsg, notice("ok", `已發布 ${r.doc_key} ${res.published}${res.retired ? `（${res.retired} 已退役）` : ""}。`));
      await loadVersions();
    } catch (err) {
      replace(pageMsg, notice("error", describeError(err)));
    }
  }

  async function loadChunks(): Promise<void> {
    if (selected === null) return;
    const sel = selected;
    const tabs = h("div", { class: "chips" }, ...(Object.keys(STATUS_LABEL) as ReviewStatus[]).map((s) =>
      h("button", { class: s === status ? "chip active" : "chip", attrs: { type: "button" },
        on: { click: () => { status = s; offset = 0; void loadChunks(); } } }, STATUS_LABEL[s])));
    replace(listBox, h("h3", {}, `${sel.doc} ${sel.version}`), tabs, h("p", { class: "note" }, "載入中…"));
    const params = new URLSearchParams({ doc: sel.doc, version: sel.version, status, offset: String(offset), limit: String(PAGE_SIZE) });
    let page: ReviewPage;
    try {
      page = await api<ReviewPage>("GET", `/admin/review/chunks?${params}`);
    } catch (err) {
      replace(listBox, h("h3", {}, `${sel.doc} ${sel.version}`), tabs, notice("error", describeError(err)));
      return;
    }
    replace(listBox, h("h3", {}, `${sel.doc} ${sel.version}`), versionPanel(page.version), tabs,
      h("p", { class: "note" }, page.total === 0 ? `沒有「${STATUS_LABEL[status]}」的片段。`
        : `共 ${page.total} 筆，顯示第 ${offset + 1}–${offset + page.items.length} 筆`),
      ...page.items.map((it) => chunkCard(it, page.flag_labels, page.version)),
      page.total > PAGE_SIZE ? h("div", { class: "pager" },
        h("button", { attrs: { type: "button", ...(offset === 0 ? { disabled: "" } : {}) },
          on: { click: () => { offset = Math.max(0, offset - PAGE_SIZE); void loadChunks(); } } }, "上一頁"),
        h("button", { attrs: { type: "button", ...(offset + PAGE_SIZE >= page.total ? { disabled: "" } : {}) },
          on: { click: () => { offset += PAGE_SIZE; void loadChunks(); } } }, "下一頁")) : null);
  }

  // 版本表「版本」欄的標記：最新版本標「最新」，否則用警告色標「非最新」並寫出最新版本。
  function latestBadge(r: VersionRow): HTMLElement {
    return r.is_latest ? h("span", { class: "badge" }, "最新")
      : h("span", { class: "badge warn" }, `非最新（最新 ${r.latest_label}）`);
  }

  // 清單上方的版本資訊：文件、本清單所屬版本與狀態、是否最新、最新草稿、目前發布版本；
  // 不是最新版本或文件已停用時，另外顯示醒目的警告，並提供「改審最新版本」。
  function versionPanel(v: VersionInfo): HTMLElement {
    const warnings: HTMLElement[] = [];
    if (!v.is_latest) {
      warnings.push(notice("warn", `注意：${v.doc_key} ${v.version_label} 不是最新版本（最新為 ${v.latest_label}）。`
        + "在這裡核准只影響這個舊版本，不會沿用到新版本；核准前會再請你確認。"));
      warnings.push(h("button", { class: "link", attrs: { type: "button" }, on: { click: () => {
        selected = { doc: v.doc_key, version: v.latest_label }; offset = 0; void loadVersions(); } } },
        `改審最新版本 ${v.latest_label}`));
    }
    if (!v.is_active) warnings.push(notice("warn", `注意：${v.doc_key} 已停用（例如改編號前的舊文件），一般不需要再審。`));
    const row = (label: string, value: string) => [h("dt", {}, label), h("dd", {}, value)];
    return h("div", {},
      h("dl", { class: "version-info", data: { versionInfo: v.version_label } },
        ...row("文件", v.doc_key + (v.is_active ? "" : "（已停用）")),
        ...row("本清單版本", v.version_label),
        ...row("版本狀態", VERSION_LABEL[v.status] ?? v.status),
        ...row("是否最新版本", v.is_latest ? "是" : `否（最新為 ${v.latest_label}）`),
        ...row("最新草稿", v.latest_draft_label ?? "沒有比發布版本新的草稿"),
        ...row("目前發布版本", v.published_label ?? "尚無發布版本")),
      ...warnings);
  }

  function chunkCard(it: ReviewItem, labels: Record<string, string>, ver: VersionInfo): HTMLElement {
    const msg = h("div");
    const reason = h("textarea", { attrs: { rows: "2", maxlength: "2000", placeholder: "理由（必填），例如：已對照 PDF p.4，文字一致", "aria-label": `${it.chunk_key} 的審核理由` } });
    const approve = h("button", { attrs: { type: "button", disabled: "" }, on: { click: () => void decide("approve") } }, "核准");
    const reject = h("button", { class: "secondary", attrs: { type: "button", disabled: "" }, on: { click: () => void decide("reject") } }, "退回");
    reason.addEventListener("input", () => {
      const ok = reason.value.trim().length >= 2;
      approve.disabled = !ok;
      reject.disabled = !ok;
    });
    // 已發布版本：退回的不能再放行；已退役版本：不可再變更
    const canApprove = it.version_status !== "retired" && (it.review_status === "pending" || it.version_status === "draft");
    const canReject = it.version_status !== "retired" && it.review_status !== "rejected";
    const history = h("div");
    const card = h("article", { class: "card chunk" },
      h("header", { class: "chunk-head" },
        h("strong", {}, it.chunk_key),
        h("span", { class: it.version_label === ver.version_label && ver.is_latest ? "badge" : "badge warn" },
          `${it.doc_key} ${it.version_label}（${VERSION_LABEL[it.version_status] ?? it.version_status}${ver.is_latest ? "，最新" : "，非最新"}）`),
        h("span", { class: "conv-meta" }, `${it.pages ? `第 ${it.pages} 頁` : it.html_locator ? `網頁段落 ${it.html_locator}` : "—"}${it.section ? ` · ${it.section}` : ""} · ${STATUS_LABEL[it.review_status]}`)),
      it.flags.length > 0 ? h("ul", { class: "flags" }, ...it.flags.map((f) =>
        h("li", { class: "badge warn", attrs: { title: f } }, labels[f] ?? f))) : null,
      it.prior_decision ? h("p", { class: "note" }, `相同內容在其他版本的審核結果：${it.prior_decision}（僅供參考，不會自動沿用）`) : null,
      h("blockquote", { class: "quote" }, it.quote_text),
      it.reviewed_by ? h("p", { class: "note" }, `${it.reviewed_by} 於 ${formatTime(it.reviewed_at)}：${it.review_note ?? ""}`) : null,
      canApprove || canReject ? h("div", { class: "review-actions" }, reason,
        h("div", { class: "row" }, canApprove ? approve : null, canReject ? reject : null)) : null,
      h("button", { class: "link", attrs: { type: "button" }, on: { click: () => void showHistory() } }, "審核紀錄"),
      history, msg);
    return card;

    // 核准前的確認框：列出將核准的文件、版本、版本狀態與片段；非最新版本另加警告，按確定才帶 confirm_not_latest。
    function confirmApprove(): boolean {
      const lines = [
        "確定核准這一筆？",
        `文件：${it.doc_key}`,
        `版本：${it.version_label}（${VERSION_LABEL[it.version_status] ?? it.version_status}）`,
        `片段：${it.chunk_key}${it.pages ? `（第 ${it.pages} 頁）` : it.html_locator ? `（${it.html_locator}）` : ""}`,
      ];
      if (!ver.is_latest) lines.push("", `⚠ 這不是最新版本（最新為 ${ver.latest_label}），核准不會沿用到新版本。`);
      return confirm(lines.join("\n"));
    }

    async function decide(decision: "approve" | "reject"): Promise<void> {
      if (decision === "approve" && !confirmApprove()) return;
      approve.disabled = reject.disabled = true;
      try {
        // 送出畫面上這張卡片的審核對象，由後端比對 chunk_id 實際所屬；不是最新版本時只有確認過才會走到這裡
        await api("POST", `/admin/review/chunks/${it.id}`, {
          decision, reason: reason.value.trim(), content_sha256: it.content_sha256,
          doc_key: it.doc_key, version_label: it.version_label, chunk_key: it.chunk_key,
          confirm_not_latest: decision === "approve" && !ver.is_latest,
        });
        replace(msg, notice("ok", decision === "approve" ? "已核准。" : "已退回。"));
        reason.disabled = true;
        await refreshCounts();
      } catch (err) {
        replace(msg, notice("error", describeError(err)));
        approve.disabled = reject.disabled = reason.value.trim().length < 2;
      }
    }

    async function showHistory(): Promise<void> {
      try {
        const ev = (await api<{ items: ReviewEvent[] }>("GET", `/admin/review/chunks/${it.id}/events`)).items;
        replace(history, ev.length === 0 ? h("p", { class: "note" }, "沒有審核紀錄。") :
          h("ul", { class: "history" }, ...ev.map((e) =>
            h("li", {}, `${formatTime(e.created_at)} ${e.reviewer}：${STATUS_LABEL[e.from_status]} → ${STATUS_LABEL[e.to_status]}，${e.reason}`))));
      } catch (err) {
        replace(history, notice("error", describeError(err)));
      }
    }
  }

  // 審核一筆後只更新上方的數量表，不重新載入清單（避免正在填寫的理由被清掉）
  async function refreshCounts(): Promise<void> {
    const rows = (await api<{ items: VersionRow[] }>("GET", "/admin/review/documents")).items;
    const body = versionsBox.querySelector("tbody");
    if (!body) return;
    rows.forEach((r, i) => {
      const tr = body.children[i];
      if (!tr) return;
      const cells = tr.children;
      if (cells[3]) cells[3].textContent = String(r.pending);
      if (cells[4]) cells[4].textContent = String(r.approved);
      if (cells[5]) cells[5].textContent = String(r.rejected);
      const pub = tr.querySelector<HTMLButtonElement>("button[data-publish]");
      if (pub) pub.disabled = r.approved === 0;
    });
  }
}
