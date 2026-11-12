// S10 管理者監控頁：統計範圍內的請求數、分類（成功／失敗／補問／資料不足）、耗時中位數與 P95、
// Token（含未知筆數）、負面回饋；可依分類或錯誤代碼列出請求，點選後查看單一請求詳情與錯誤。
//
// 權限：只有管理者看得到導覽連結；真正的把關在後端（/api/admin/monitor/* 需要管理者 session）。
// 隱私：後端本來就不回使用者問題、回答、模型輸出與回饋自由說明，秘密欄位已遮蔽；
//       這裡所有內容一律以文字節點顯示（dom.h），不使用 innerHTML。
// 未知：Token 與耗時為 null 時顯示「未知」，不顯示成 0（規範 8）。

import { api, describeError } from "../api.js";
import { formatTime, h, notice, replace } from "../dom.js";

// ---------------------------------------------------------------------------
// API 回應型別（只標註畫面用到的欄位；內容一律不信任，顯示時都轉成文字）
// ---------------------------------------------------------------------------
type CategoryKey = "success" | "failed" | "clarification" | "insufficient_data" | "in_progress";

interface RangeInfo {
  since: string;
  until: string;
  timezone: string;
  generated_at?: string;
}

interface Summary {
  range: RangeInfo;
  requests: {
    total: number;
    categories: { key: CategoryKey; label: string; n: number; rate: number | null }[];
    by_outcome: { category: CategoryKey; outcome: string; n: number }[];
    by_error: { error_code: string; stage: string | null; n: number; last_seen: string }[];
  };
  latency_ms: { n: number; p50: number | null; p95: number | null; max: number | null };
  tokens: {
    input_sum: number | null; input_known: number; input_unknown: number;
    output_sum: number | null; output_known: number; output_unknown: number;
    model_requests: number;
  };
  feedback: {
    positive: number;
    negative: number;
    negative_reasons: { code: string; n: number }[];
    recent_negative: { request_id: string; updated_at: string; reason_codes: string[];
      option_key: string | null; has_comment: boolean; outcome: string | null }[];
  };
  definitions: { time_field: string; categories: Record<string, string>; latency: string; tokens: string;
    privacy: string };
}

interface RequestRow {
  request_id: string;
  user_id: number;
  started_at: string;
  status: string;
  category: CategoryKey;
  outcome: string | null;
  intent: string | null;
  error_code: string | null;
  latency_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  model_name: string | null;
  feedback_rating: number | null;
}

// 詳情欄位多且會隨日誌演進，這裡只用寬鬆型別，畫面上逐欄位轉文字
type Detail = Record<string, unknown>;

interface Filters {
  category: CategoryKey | null;
  errorCode: string | null;
}

const CATEGORY_LABEL: Record<CategoryKey, string> = {
  success: "成功", failed: "失敗", clarification: "補問", insufficient_data: "資料不足", in_progress: "未完成",
};

// 預設的統計範圍選項（小時）
const PRESETS: { label: string; hours: number }[] = [
  { label: "最近 24 小時", hours: 24 },
  { label: "最近 7 天", hours: 24 * 7 },
  { label: "最近 30 天", hours: 24 * 30 },
];

const FEEDBACK_REASON_LABEL: Record<string, string> = {
  not_fit: "不符合我的條件", source_unclear: "來源看不懂或對不上", too_generic: "太籠統",
  missed_exclusion: "沒有排除我不要的", other: "其他", fits_preference: "符合我的條件",
  clear_sources: "來源清楚", useful_alternative: "替代方向有幫助",
};

// ---------------------------------------------------------------------------
// 小工具
// ---------------------------------------------------------------------------
/** 數字或「未知」：null／undefined 一律顯示未知，不顯示 0。 */
function orUnknown(value: unknown, unit = ""): string {
  return value === null || value === undefined ? "未知" : `${String(value)}${unit}`;
}

/** 內容字數：null 表示沒有這項內容（不是未知），顯示「無」。 */
function chars(value: unknown): string {
  return value === null || value === undefined ? "無" : `${String(value)} 字`;
}

/** 比例轉百分比文字；分母為 0（後端回 null）顯示「—」。 */
function percent(rate: number | null): string {
  return rate === null ? "—" : `${(rate * 100).toFixed(1)}%`;
}

/** 任意值轉成可顯示的文字（物件轉成縮排 JSON）。 */
function text(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  return String(value);
}

/** 把 ISO 時間轉成查詢字串參數。 */
function rangeQuery(since: Date, until: Date): string {
  return `since=${encodeURIComponent(since.toISOString())}&until=${encodeURIComponent(until.toISOString())}`;
}

/** <input type="datetime-local"> 需要的本地時間字串（yyyy-MM-ddTHH:mm）。 */
function toLocalInput(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** 建立表格：headers 為欄名，rows 為每列的儲存格（字串或節點）。 */
function table(headers: string[], rows: (string | Node)[][]): HTMLElement {
  return h("div", { class: "table-wrap" },
    h("table", { class: "table" },
      h("thead", {}, h("tr", {}, ...headers.map((t) => h("th", {}, t)))),
      h("tbody", {}, ...rows.map((r) => h("tr", {}, ...r.map((c) => h("td", {}, c)))))));
}

/** 名稱／值的定義清單。 */
function facts(pairs: [string, string | Node][]): HTMLElement {
  const dl = h("dl", { class: "facts" });
  for (const [k, v] of pairs) dl.append(h("dt", {}, k), h("dd", {}, v));
  return dl;
}

// ---------------------------------------------------------------------------
// 頁面
// ---------------------------------------------------------------------------
/** 繪製監控頁：範圍選擇、摘要、請求清單、詳情。 */
export async function renderMonitor(view: HTMLElement): Promise<void> {
  const rangeBox = h("div", { class: "card" });
  const summaryBox = h("div");
  const listBox = h("div", { class: "card" });
  const detailBox = h("div");
  replace(view, h("h2", {}, "監控"),
    notice("info", "管理者限定。統計只含日誌中的中繼資料：不顯示使用者問題、回答、模型原始輸出與回饋說明原文，"
      + "秘密欄位已遮蔽；Token 未取得時顯示「未知」，不記為 0。"),
    rangeBox, summaryBox, listBox, detailBox);

  let until = new Date();
  let since = new Date(until.getTime() - PRESETS[1]!.hours * 3600 * 1000);
  const filters: Filters = { category: null, errorCode: null };

  renderRange();
  await refresh();

  // 範圍選擇列：預設區間按鈕＋自訂起訖（本地時間輸入，送出時轉 UTC）。
  function renderRange(): void {
    const sinceInput = h("input", { attrs: { type: "datetime-local", value: toLocalInput(since), "aria-label": "開始時間" } });
    const untilInput = h("input", { attrs: { type: "datetime-local", value: toLocalInput(until), "aria-label": "結束時間" } });
    const msg = h("div");
    replace(rangeBox,
      h("h3", {}, "統計時間範圍"),
      h("div", { class: "chips" }, ...PRESETS.map((p) => h("button", {
        class: "chip", attrs: { type: "button" },
        on: { click: () => { until = new Date(); since = new Date(until.getTime() - p.hours * 3600 * 1000);
          renderRange(); void refresh(); } },
      }, p.label))),
      h("div", { class: "row range-row" }, sinceInput, h("span", {}, "至"), untilInput,
        h("button", { class: "secondary", attrs: { type: "button" }, on: { click: () => {
          const s = new Date(sinceInput.value);
          const u = new Date(untilInput.value);
          if (Number.isNaN(s.getTime()) || Number.isNaN(u.getTime()) || s >= u) {
            replace(msg, notice("error", "請輸入有效的開始與結束時間，且開始要早於結束。"));
            return;
          }
          since = s; until = u; void refresh();
        } } }, "套用")),
      msg);
  }
  // 核心邏輯：datetime-local 是瀏覽器本地時間；new Date() 解析後以 toISOString() 送出 UTC，
  // 後端回傳的範圍再以 UTC 與本地時間並列顯示，避免時區誤會。

  // 重新讀取摘要與清單；切換範圍時清掉舊的詳情。
  async function refresh(): Promise<void> {
    replace(detailBox);
    await Promise.all([loadSummary(), loadList()]);
  }

  async function loadSummary(): Promise<void> {
    replace(summaryBox, notice("info", "讀取統計中…"));
    let s: Summary;
    try {
      s = await api<Summary>("GET", `/admin/monitor/summary?${rangeQuery(since, until)}`);
    } catch (err) {
      replace(summaryBox, notice("error", describeError(err)));
      return;
    }
    replace(summaryBox, ...summaryCards(s));
  }

  // 摘要卡片：範圍、請求分類、耗時、Token、結果與錯誤細分、回饋。
  function summaryCards(s: Summary): HTMLElement[] {
    const r = s.requests;
    const t = s.tokens;
    const rangeCard = h("div", { class: "card" },
      h("h3", {}, "本次統計範圍"),
      facts([
        ["開始（UTC）", s.range.since],
        ["結束（UTC）", s.range.until],
        ["本地時間", `${formatTime(s.range.since)} 至 ${formatTime(s.range.until)}`],
        ["產生時間", formatTime(s.range.generated_at)],
        ["時間欄位", s.definitions.time_field],
      ]));
    const catCard = h("div", { class: "card" },
      h("h3", {}, `請求數：${r.total}`),
      table(["分類", "筆數", "比例", "口徑"], r.categories.map((c) => [
        h("button", { class: "link", attrs: { type: "button" },
          on: { click: () => { filters.category = c.key; filters.errorCode = null; void loadList(); } } }, c.label),
        String(c.n), percent(c.rate), s.definitions.categories[c.key] ?? "",
      ])),
      r.total === 0 ? notice("info", "這段時間沒有任何請求。") : null);
    const perfCard = h("div", { class: "card" },
      h("h3", {}, "耗時與用量"),
      facts([
        ["耗時中位數", orUnknown(s.latency_ms.p50, " ms")],
        ["耗時 P95", orUnknown(s.latency_ms.p95, " ms")],
        ["最長", orUnknown(s.latency_ms.max, " ms")],
        ["計入筆數", `${s.latency_ms.n}（${s.definitions.latency}）`],
        ["輸入 Token", `${orUnknown(t.input_sum)}（已知 ${t.input_known} 筆／未知 ${t.input_unknown} 筆）`],
        ["輸出 Token", `${orUnknown(t.output_sum)}（已知 ${t.output_known} 筆／未知 ${t.output_unknown} 筆）`],
        ["有呼叫模型的請求", String(t.model_requests)],
      ]),
      h("p", { class: "note" }, s.definitions.tokens),
      s.latency_ms.n > 0 && s.latency_ms.n < 20
        ? notice("info", `計入耗時的請求只有 ${s.latency_ms.n} 筆，P95 參考價值有限。`) : null);
    const outcomeCard = h("div", { class: "card" },
      h("h3", {}, "結果細分"),
      r.by_outcome.length === 0 ? h("p", { class: "note" }, "—")
        : table(["分類", "outcome", "筆數"], r.by_outcome.map((o) => [CATEGORY_LABEL[o.category] ?? o.category,
          o.outcome, String(o.n)])),
      h("h4", {}, "錯誤"),
      r.by_error.length === 0 ? h("p", { class: "note" }, "這段時間沒有錯誤。")
        : table(["錯誤代碼", "階段", "筆數", "最近一次"], r.by_error.map((e) => [
          h("button", { class: "link mono", attrs: { type: "button" },
            on: { click: () => { filters.errorCode = e.error_code; filters.category = null; void loadList(); } } },
          e.error_code),
          e.stage ?? "—", String(e.n), formatTime(e.last_seen)])));
    const fb = s.feedback;
    const fbCard = h("div", { class: "card" },
      h("h3", {}, `回饋：有幫助 ${fb.positive}／沒幫助 ${fb.negative}`),
      fb.negative_reasons.length === 0 ? h("p", { class: "note" }, "沒有負面回饋原因。")
        : table(["負面原因", "次數"], fb.negative_reasons.map((x) => [FEEDBACK_REASON_LABEL[x.code] ?? x.code,
          String(x.n)])),
      fb.recent_negative.length === 0 ? null : h("h4", {}, "最近的負面回饋"),
      fb.recent_negative.length === 0 ? null
        : table(["時間", "request", "結果", "方向", "原因", "有文字說明"], fb.recent_negative.map((x) => [
          formatTime(x.updated_at), requestLink(x.request_id), x.outcome ?? "—", x.option_key ?? "—",
          x.reason_codes.map((c) => FEEDBACK_REASON_LABEL[c] ?? c).join("、") || "—",
          x.has_comment ? "有（原文不在監控頁）" : "無"])));
    return [rangeCard, catCard, perfCard, outcomeCard, fbCard];
  }
  // 核心邏輯：分類列與錯誤代碼可以點，點了就用它篩選下方清單；
  // 全部數字都直接取自後端，前端不自行重算比例或百分位，避免兩邊口徑不一。

  // 可點的 request_id：點了在下方顯示詳情。
  function requestLink(id: string): HTMLElement {
    return h("button", { class: "link mono", attrs: { type: "button" }, on: { click: () => void loadDetail(id) } },
      id.slice(0, 8));
  }

  // 讀取請求清單（套用目前的分類／錯誤代碼篩選）。
  async function loadList(): Promise<void> {
    const q = [rangeQuery(since, until), "limit=100"];
    if (filters.category) q.push(`category=${filters.category}`);
    if (filters.errorCode) q.push(`error_code=${encodeURIComponent(filters.errorCode)}`);
    replace(listBox, h("h3", {}, "請求清單"), notice("info", "讀取中…"));
    let rows: RequestRow[];
    try {
      rows = (await api<{ items: RequestRow[] }>("GET", `/admin/monitor/requests?${q.join("&")}`)).items;
    } catch (err) {
      replace(listBox, h("h3", {}, "請求清單"), notice("error", describeError(err)));
      return;
    }
    const label = filters.category ? `分類：${CATEGORY_LABEL[filters.category]}`
      : filters.errorCode ? `錯誤代碼：${filters.errorCode}` : "全部";
    replace(listBox,
      h("h3", {}, `請求清單（${label}，最多 100 筆，新到舊）`),
      filters.category || filters.errorCode
        ? h("button", { class: "link", attrs: { type: "button" },
          on: { click: () => { filters.category = null; filters.errorCode = null; void loadList(); } } }, "清除篩選")
        : null,
      rows.length === 0 ? notice("info", "沒有符合的請求。")
        : table(["開始時間", "request", "分類", "outcome", "意圖", "錯誤", "耗時", "Token 入／出", "回饋"],
          rows.map((x) => [
            formatTime(x.started_at), requestLink(x.request_id), CATEGORY_LABEL[x.category] ?? x.category,
            x.outcome ?? "—", x.intent ?? "—", x.error_code ?? "—", orUnknown(x.latency_ms, " ms"),
            `${orUnknown(x.input_tokens)}／${orUnknown(x.output_tokens)}`,
            x.feedback_rating === null ? "—" : x.feedback_rating > 0 ? "有幫助" : "沒幫助",
          ])));
  }

  // 讀取並顯示單一請求詳情。
  async function loadDetail(id: string): Promise<void> {
    replace(detailBox, h("div", { class: "card" }, notice("info", "讀取詳情中…")));
    let d: Detail;
    try {
      d = await api<Detail>("GET", `/admin/monitor/requests/${encodeURIComponent(id)}`);
    } catch (err) {
      replace(detailBox, h("div", { class: "card" }, notice("error", describeError(err))));
      return;
    }
    replace(detailBox, renderDetail(d));
    detailBox.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

/** 單一請求詳情卡片：基本資料、錯誤、解析呼叫、推薦、問答、召回、回饋。 */
function renderDetail(d: Detail): HTMLElement {
  const get = (k: string): unknown => d[k];
  const list = (k: string): Record<string, unknown>[] => (Array.isArray(d[k]) ? d[k] as Record<string, unknown>[] : []);
  const failed = get("status") === "failed";
  const parse = list("parse_calls");
  const hits = list("retrieval_hits");
  const recs = list("recommendations");
  const rec = get("recommendation") as Record<string, unknown> | null;
  const qa = get("qa") as Record<string, unknown> | null;
  const fb = get("feedback") as Record<string, unknown> | null;
  return h("div", { class: "card" },
    h("h3", {}, "請求詳情"),
    facts([
      ["request_id", h("span", { class: "mono" }, text(get("request_id")))],
      ["使用者 id", text(get("user_id"))],
      ["狀態／分類", `${text(get("status"))}／${CATEGORY_LABEL[get("category") as CategoryKey] ?? text(get("category"))}`],
      ["outcome", text(get("outcome"))],
      ["意圖", `${text(get("intent"))}${list("secondary_intents").length ? `（另含 ${text(get("secondary_intents"))}）` : ""}`],
      ["開始／完成", `${formatTime(get("started_at") as string)}／${formatTime(get("completed_at") as string)}`],
      ["耗時", orUnknown(get("latency_ms"), " ms")],
      ["重試次數", text(get("retry_count"))],
      ["模型", `${text(get("model_provider"))} ${text(get("model_name"))}`],
      ["Token 入／出", `${orUnknown(get("input_tokens"))}／${orUnknown(get("output_tokens"))}`],
      ["版本", `app ${text(get("app_version"))}、prompt ${text(get("prompt_version"))}、索引 ${text(get("index_label"))}`],
      ["回答來源", text(get("answer_source"))],
      // 字數為 null 代表這次本來就沒有該內容（例如沒有產生回答），顯示「無」而不是「未知」
      ["內容字數", `問題 ${chars(get("question_chars"))}、回答 ${chars(get("answer_chars"))}、`
        + `模型輸出 ${chars(get("model_output_chars"))}（原文不在監控頁）`],
      ["補問／限制", `${text(get("clarification_count"))} 題／${text(get("limitations_count"))} 項`],
    ]),
    failed || get("error_code")
      ? h("div", {}, h("h4", {}, "錯誤"),
        notice("error", `${text(get("error_code"))}`),
        h("pre", { class: "quote mono" }, text(get("error_detail"))))
      : null,
    h("h4", {}, "解析呼叫"),
    parse.length === 0 ? h("p", { class: "note" }, "—")
      : table(["階段", "解析器", "版本", "模型", "Token 入／出", "耗時", "錯誤"], parse.map((c) => [
        text(c["stage"]), text(c["parser"]), text(c["parser_version"]), text(c["model_name"]),
        `${orUnknown(c["input_tokens"])}／${orUnknown(c["output_tokens"])}`, orUnknown(c["latency_ms"], " ms"),
        text(c["error"])])),
    h("h4", {}, "推薦"),
    rec === null ? h("p", { class: "note" }, "這次沒有進入推薦流程。")
      : h("div", {},
        facts([["狀態", text(rec["status"])], ["規則版本", text(rec["rules_version"])],
          ["候選數", text(rec["candidate_count"])], ["合成資料", rec["synthetic"] ? "是（SYNTHETIC）" : "否"],
          ["耗時", orUnknown(rec["latency_ms"], " ms")], ["錯誤", text(rec["error"])]]),
        recs.length === 0 ? null : table(["順位", "候選", "候選狀態", "引用數"], recs.map((x) => [
          x["rank"] === 1 ? "主推" : `替代 ${Number(x["rank"]) - 1}`, text(x["option_key"]), text(x["option_status"]),
          text(x["citations"])])),
        h("details", {}, h("summary", {}, "取捨紀錄（trace）"), h("pre", { class: "quote mono" }, text(rec["trace"])))),
    h("h4", {}, "知識問答"),
    qa === null ? h("p", { class: "note" }, "這次沒有進入問答流程。")
      : h("div", {},
        facts([["結果", text(qa["outcome"])], ["錯誤", text(qa["error_code"])], ["prompt", text(qa["prompt_version"])],
          ["可用片段數", text(qa["usable_chunks_for_role"])], ["主張數", text(qa["claims_count"])]]),
        h("details", {}, h("summary", {}, "檢索／模型呼叫／驗證（已遮蔽秘密）"),
          h("pre", { class: "quote mono" }, text({ retrieval: qa["retrieval"], model_calls: qa["model_calls"],
            generation: qa["generation"], final_revalidate: qa["final_revalidate"], config: qa["config"],
            timings_ms: qa["timings_ms"], context_evidence: qa["context_evidence"], citations: qa["citations"] })))),
    h("h4", {}, "召回紀錄"),
    hits.length === 0 ? h("p", { class: "note" }, "—")
      : table(["方法", "名次", "片段", "文件／版本", "分數", "通過檢查", "擋下原因"], hits.map((x) => [
        text(x["retriever"]), text(x["rank"]), h("span", { class: "mono" }, text(x["chunk_key"])),
        `${text(x["doc_key"])} ${text(x["version_label"])}`, text(x["score"]), x["passed_filter"] ? "是" : "否",
        text(x["filter_reason"])])),
    h("h4", {}, "回饋"),
    fb === null ? h("p", { class: "note" }, "沒有回饋。")
      : facts([["評價", fb["rating"] === 1 ? "有幫助" : "沒幫助"], ["方向", text(fb["option_key"])],
        ["原因", Array.isArray(fb["reason_codes"])
          ? (fb["reason_codes"] as string[]).map((c) => FEEDBACK_REASON_LABEL[c] ?? c).join("、") || "—" : "—"],
        ["文字說明", fb["has_comment"] ? "有（原文不在監控頁）" : "無"], ["更新時間", formatTime(fb["updated_at"] as string)]]));
}
// 核心邏輯：詳情欄位逐一轉成文字顯示；JSON 區塊用 <pre> 的 textContent，內容即使含 HTML 也只會顯示成字。
