// S8 推薦結果區塊：一次請求的結果（主要／替代推薦、理由與引用、未符合偏好、限制、補問），
// 引用來源的原文與定位查看，以及掛在 request_id 上的回饋表單。
// 2026-10-20 聊天工作區改版（日常清新）：結果改以 AI 回覆泡泡＋推薦卡片呈現，回饋改為細線圖示＋展開表單；
// 引用查核、權限回收（recheckCitations）與回饋 API 的行為不變。
//
// 資料來源：GET /api/requests/{request_id}（後端已移除取捨紀錄 trace 等受限資訊，引用也已依角色重新判斷）。
// 安全：所有文字都用 textContent 顯示（dom.ts 的 h()），來源原文只當資料，不解讀成 HTML 或指令；
//       前端不自行判斷權限，看不到的來源由後端回 404，畫面只顯示「目前無法查看」。

import { api, ApiError, describeError } from "../api.js";
import { formatTime, h, notice, replace } from "../dom.js";
import { icon } from "../icons.js";

/** 引用定位（available=false 時後端已清掉定位資訊，只剩 note；問答引用另保留 number）。 */
export interface Citation {
  available: boolean;
  note?: string;
  number?: number;
  chunk_key?: string;
  doc_key?: string;
  version_label?: string;
  title?: string;
  /** citation-label-v1：刊物／學校／網站名稱與出版年份（來自書目；沒有就不給）。 */
  venue?: string | null;
  year?: number | string | null;
  page_start?: number | null;
  page_end?: number | null;
  html_locator?: string | null;
  /** S9：當時引用的版本已被取代、但目前版本有相同內容時為 true；cited_* 是原本引用的版本與片段。 */
  superseded?: boolean;
  cited_version_label?: string;
  cited_chunk_key?: string;
}

/** 推薦理由的一條主張；claim_type 決定是研究觀察、造型建議、你的偏好或預設條件。 */
interface Claim {
  claim_type: string;
  text: string;
  about?: string;
  citations: Citation[];
}

/** rec-v2：條件式方向的來源適用條件（底調未知時「如果你是冷底…」）。 */
interface RecCondition {
  undertones: string[];
  labels: string[];
  text: string;
}

/** 一個推薦方向（rank 1 主推、2–3 替代；conditional 狀態下每張都是條件式方向）。 */
interface RecItem {
  rank: number;
  role: "primary" | "alternative" | "conditional";
  condition?: RecCondition | null;
  option_key: string;
  name: string;
  description: string;
  reason: string;
  claims: Claim[];
  unmet: { field: string; labels: string[] }[];
  tied_with: string[];
  limitations: string[];
  synthetic: boolean;
}

interface Condition {
  label: string;
}

/** brief-v1：使用者看的精簡呈現（一句總結＋每個方向一句原因；原因是已核對主張原文）。 */
interface Brief {
  headline: string | null;
  covered: string[];
  items: { rank: number; role: string; name: string; condition: string | null; why: string | null }[];
}

/** 後端清理過的推薦結果（results.public_recommendation）。 */
interface Recommendation {
  brief?: Brief | null;
  status: "recommended" | "conditional" | "no_match" | "insufficient_evidence" | "needs_clarification";
  reason: string | null;
  recommendations: RecItem[];
  hard_conditions: Condition[];
  assumptions: { text: string }[];
  limitations: string[];
  question: string | null;
  synthetic: boolean;
}

interface Feedback {
  rating: 1 | -1;
  reason_codes: string[];
  comment: string | null;
  rank: number | null;
  updated_at: string;
}

/** S5 問答結果（後端 results.public_qa：只含定位，原文經引用端點查看）。 */
interface QaView {
  outcome: string;
  citations: Citation[];
  content_support: "not_verified" | null;
}

/** GET /api/requests/{id} 的回應。 */
export interface RequestView {
  request_id: string;
  question: string;
  request_status: "started" | "succeeded" | "failed";
  status: string | null;
  message: string | null;
  intent: string | null;
  secondary_intents: string[];
  error_code: string | null;
  answer: string | null;
  applied_changes: { field: string; field_label?: string; op: string; label: string; raw: string | null }[];
  clarification: { question: string; blocking: boolean }[];
  limitations: string[];
  recommendation: Recommendation | null;
  qa: QaView | null;
  /** S9：此刻有任何依據已不可用（停用、過期、改權限、內容更新）時，後端整則遮蔽，只給說明。 */
  masked: boolean;
  masked_note: string | null;
  started_at: string;
  latency_ms: number | null;
  feedback: Feedback | null;
  feedback_reasons: Record<string, string>;
}

/** 回饋送出後的回應。 */
interface FeedbackSaved {
  rating: 1 | -1;
  rank: number | null;
  updated_at: string;
}

// 結果狀態（requests.detail.outcome）→ 使用者看得懂的標題
export const OUTCOME_LABEL: Record<string, string> = {
  recommended: "已產生推薦",
  conditional: "依底調條件列出方向",
  no_match: "沒有符合全部條件的方向",
  insufficient_evidence: "證據不足，不提供推薦",
  needs_clarification: "需要先確認",
  preferences_updated: "已更新條件",
  outside_scope: "範圍外",
  // route-v3（2026-11-01）：商品要求、索取非公開資料改走攔截出口，不出推薦卡
  product_not_enabled: "商品功能未開放",
  restricted_request: "無權限提供",
  sources_only: "AI 暫時無法回答（只列原文）",
  not_answered: "未產生回答（模型尚未啟用）",
  parse_failed: "解析失敗",
  recommendation_error: "推薦依據變動，這次不回傳",
  answered: "已回答（有來源）",
  answer_insufficient_evidence: "來源不足，未回答",
  answer_failed: "回答失敗",
  // wording-entry-v1：只整理使用者自己的文字（沒有查資料、沒有來源）；防呆：回答卻沒有任何引用
  wording_only: "已整理你的文字（未查資料）",
  answered_without_citations: "已回答（沒有來源引用）",
};
const CLAIM_LABEL: Record<string, string> = {
  research_observation: "研究觀察", styling_advice: "造型建議", product_rule: "商品屬性",
  personal_preference: "你的偏好", transparent_default: "預設條件",
};
const FIELD_LABEL: Record<string, string> = {
  effects: "希望效果", liked_finishes: "喜歡的質地", liked_colors: "喜歡的色系", occasion: "場合",
};
const OP_LABEL: Record<string, string> = { add: "加入", remove: "移除", set: "設定", replace: "改為" };

/**
 * 讀取並畫出一次請求的結果；載入中、錯誤都有明確狀態。
 * @param box 要放結果的容器
 * @param requestId 請求 id
 * @param onFeedback 回饋送出成功後呼叫（讓需求紀錄同步顯示回饋）
 */
export async function showRequestResult(box: HTMLElement, requestId: string, onFeedback?: () => void): Promise<void> {
  replace(box, h("p", { class: "note", attrs: { role: "status", "aria-busy": "true" } }, "正在讀取結果…"));
  try {
    const view = await api<RequestView>("GET", `/requests/${encodeURIComponent(requestId)}`);
    replace(box, renderRequestView(view, onFeedback));
    watchCitations(box, requestId);
  } catch (err) {
    replace(box, notice("error", `結果讀取失敗：${describeError(err)}`));
  }
}

/**
 * 一次請求的完整結果（聊天工作區的 AI 回覆）：
 * 狀態標籤 → 回覆泡泡 → 推薦卡片或回答 → 補問 → 更新的條件 → 限制 → 回饋操作列與時間／request_id。
 * 外層的 AI 頭像與左右對齊由 chat.ts 負責；這裡只畫內容，讓重新讀取時可以整塊替換。
 * @param view 後端回傳的請求結果
 * @param onFeedback 回饋送出成功後的回呼
 */
export function renderRequestView(view: RequestView, onFeedback?: () => void): HTMLElement {
  const rec = view.recommendation;
  const outcome = view.status ?? "";
  const failed = view.request_status === "failed";
  const kind = failed || outcome === "needs_clarification" ? "error"
    : outcome === "recommended" || outcome === "conditional" || outcome === "answered" ? "ok" : "info";
  // brief-v1（使用者 2026-10-25 真人判定）：已推薦時只顯示一句總結（中性底等），不顯示規則說明長句
  const brief = rec?.brief && !view.masked ? rec.brief : null;
  // 已回答的問答：不顯示「每一句都標了來源編號…」的說明（本文已不顯示來源標記；核對說明在參考依據裡）；
  // notice-v4（2026-11-05，I04）：同一則說明裡的其他提醒（例如權限、指令類要求無法照做）照樣顯示
  const lead = brief && outcome === "recommended" ? brief.headline
    : outcome === "answered" && !view.masked ? answeredLead(view) : leadText(view);
  // rec-v2/H06：只因來源的底調條件不符而沒有方向時，標籤不寫成「條件」不符（不是使用者的排除條件）
  const tag = outcome === "no_match" && rec?.reason === "undertone_condition_not_met"
    ? "沒有適用於你底調的來源方向" : OUTCOME_LABEL[outcome] ?? (failed ? "處理失敗" : "已處理");
  return h("section", { class: "result cw-result", attrs: { "aria-live": "polite" } },
    h("p", { class: "cw-status-tag", data: { kind } }, tag),
    lead ? h("p", { class: "cw-bubble cw-bubble-ai" }, lead) : null,
    rec && !view.masked ? renderRecommendation(rec) : null,
    !rec && view.answer
      ? (outcome === "sources_only" ? renderSourcesOnly(view.answer, view.qa) : renderAnswer(view.answer, view.qa))
      : null,
    renderClarification(view),
    renderChanges(view),
    renderLimitations(uniqueLimitations(view)),
    renderFeedback(view, onFeedback));
}

/**
 * brief-v1「參考依據」小按鈕：來源、原文、限制等細節預設收起，點開才顯示。
 * 使用者 2026-10-25 判定：回答本體只要結論與做法，來源依據放在旁邊的小按鈕即可。
 */
function refToggle(label: string, ...content: (HTMLElement | null)[]): HTMLElement {
  const id = `ref-${Math.random().toString(36).slice(2, 10)}`;
  const body = h("div", { class: "cw-claims", attrs: { id } }, ...content);
  body.hidden = true;
  const btn = h("button", {
    class: "cw-source-toggle",
    attrs: { type: "button", "aria-expanded": "false", "aria-controls": id },
  }, h("span", {}, label), icon("arrowRight"));
  btn.addEventListener("click", () => {
    const open = btn.getAttribute("aria-expanded") !== "true";
    btn.setAttribute("aria-expanded", String(open));
    body.hidden = !open;
  });
  return h("div", {}, btn, body);
}

/**
 * 限制說明去重：後端有時把同一句話同時放在推薦的「前提」（assumptions）與整體「限制」（limitations），
 * 前提已顯示在推薦區塊時，限制說明不再重複列出同一句；只比對完全相同的文字，不改寫或合併內容。
 * 整則遮蔽（masked）時推薦區塊不顯示，前提也不會出現，因此限制一律完整保留。
 */
function uniqueLimitations(view: RequestView): string[] {
  const rec = view.recommendation;
  if (!rec || view.masked) return view.limitations;
  const shown = new Set(rec.assumptions.map((a) => a.text.trim()));
  return view.limitations.filter((l) => !shown.has(l.trim()));
}

/**
 * 回覆泡泡的文字：遮蔽說明優先（S9：整則遮蔽時不顯示原本的結果說明），
 * 其次是後端的結果說明；都沒有且沒有回答本文時，以狀態名稱代替，避免出現空白泡泡。
 */
function leadText(view: RequestView): string | null {
  if (view.masked) return view.masked_note ?? "原回答已隱藏。";
  if (view.message) return view.message;
  if (view.answer) return null;
  return OUTCOME_LABEL[view.status ?? ""] ?? null;
}

/** 後端已回答說明的固定開頭（main.py _OUTCOME_MESSAGES["answered"]）；只用來在畫面上把這一句拿掉。 */
const ANSWERED_NOTE_PREFIX = "以下回答只根據已核准的來源片段整理";

/** 證據不足附一般建議時，回答第一行的固定開頭（exit_assist.INSUFFICIENT_LEAD_PREFIX）。 */
const INSUFFICIENT_LEAD_PREFIX = "沒有找到可以回答的資料：";

/**
 * 已回答時回覆泡泡的文字：說明逐行看，拿掉固定的來源說明句，留下其他提醒；都沒有就不顯示泡泡。
 */
function answeredLead(view: RequestView): string | null {
  const lines = (view.message ?? "").split("\n")
    .filter((line) => line.trim() && !line.startsWith(ANSWERED_NOTE_PREFIX));
  return lines.length ? lines.join("\n") : null;
}

/** 複製用的純文字：回覆說明＋各推薦方向（名稱、說明）＋回答本文；不含來源原文。 */
function copyText(view: RequestView): string {
  const lines: string[] = [];
  const lead = leadText(view);
  if (lead) lines.push(lead);
  if (!view.masked) {
    for (const r of shownRecs(view.recommendation)) {
      lines.push(`${roleTitle(r)}：${r.name}${r.description ? `｜${r.description}` : ""}`);
    }
    if (view.answer) lines.push(view.answer);
  }
  return lines.join("\n");
}

/**
 * S5 知識問答的回答與來源。回答是後端依已驗證主張組成的純文字（句尾〔來源：出處〕，舊紀錄為〔來源 n〕）；
 * 每個來源用與推薦相同的引用按鈕：點擊時向後端重新查詢原文，失效或無權限時只顯示「目前無法查看」。
 */
function renderAnswer(answer: string, qa: QaView | null): HTMLElement {
  const cites = qa?.citations ?? [];
  // brief-v1（使用者 2026-10-25 真人判定）：本文不顯示〔來源 n〕標記；「限制：」行收進「參考依據」；
  // 後端沒給「出處：」行時，用引用的文件標題補一行。來源清單與核對說明收進「參考依據」。
  // citation-label-v1：本文標記改成〔來源：〈標題〉…〕，舊紀錄仍是〔來源 n〕，兩種都要隱藏
  const all = answer.split("\n").map((l) => l.replace(/〔來源[ ：][^〕]*〕/g, "").trim()).filter((l) => l.length > 0);
  const notes = all.filter((l) => l.startsWith("限制："));
  const body = all.filter((l) => !l.startsWith("限制："));
  // 出處行列文章標題＋刊物＋年份（不含頁碼；頁碼寫在「參考依據」各來源上）
  const titles = [...new Set(cites.filter((c) => c.title).map((c) => sourceLabel(c, false)))];
  // insufficient-lead-v1（2026-11-05，P08）：證據不足時開頭那句「沒有找到可以回答的資料：…」要留在最前面，
  // 出處行（一般建議用到的來源）接在它後面
  const at = body[0]?.startsWith(INSUFFICIENT_LEAD_PREFIX) ? 1 : 0;
  if (!body[at]?.startsWith("出處：") && titles.length > 0) body.splice(at, 0, `出處：${titles.join("；")}`);
  return h("div", { class: "answer cw-answer" },
    ...body.map((line) => line.startsWith("出處：") ? h("p", { class: "cw-hint" }, line) : h("p", {}, line)),
    cites.length > 0 ? refToggle(`參考依據（${cites.length}）`,
      notes.length > 0 ? h("ul", { class: "plain cw-hint" }, ...notes.map((l) => h("li", {}, l))) : null,
      h("ol", { class: "cw-answer-sources" }, ...cites.map((c) => {
        const viewer = h("div");
        return h("li", { attrs: { value: String(c.number ?? "") } }, citationButton(c, viewer), viewer);
      }))) : null);
}

/** 只列原文時，後端回答第一行的固定說明（qa.SOURCES_ONLY_HEAD）；回覆泡泡已顯示同一句，本文不再重複。 */
const SOURCES_ONLY_HEAD_PREFIX = "AI 暫時無法回答";

/** 一段文字是不是以英文為主（中日韓字元少於字母的兩成）；只用來加「英文原文」提示，不改內容。 */
function mostlyLatin(text: string): boolean {
  const cjk = (text.match(/[㐀-鿿]/g) ?? []).length;
  const latin = (text.match(/[A-Za-z]/g) ?? []).length;
  return latin > 0 && cjk < latin * 0.2;
}

/**
 * sources-only-v2（2026-11-07）：模型連不上時只列原文。
 * 原本本文第一行又重複「AI 暫時無法回答…」、原文段落沒標出處、英文段落沒有提示；
 * 改成：拿掉重複的說明句，每段先標「來源 n：〈標題〉…」，英文為主的段落加「英文原文」標記。
 * 原文仍照後端給的文字顯示（textContent），不翻譯、不摘要。
 */
function renderSourcesOnly(answer: string, qa: QaView | null): HTMLElement {
  const cites = qa?.citations ?? [];
  // 後端舊紀錄的 number 可能是字串（"2"），一律用字串比對
  const byNumber = new Map(cites.filter((c) => c.number != null).map((c) => [String(c.number), c]));
  const blocks: HTMLElement[] = [];
  for (const raw of answer.split("\n")) {
    const line = raw.trim();
    if (!line || line.startsWith(SOURCES_ONLY_HEAD_PREFIX)) continue;
    const m = /^〔來源 (\d+)〕([\s\S]*)$/.exec(line);
    if (!m) {
      blocks.push(h("p", { class: "cw-hint" }, line));
      continue;
    }
    const c = byNumber.get(m[1]);
    const text = m[2].trim();
    blocks.push(h("div", { class: "cw-source-only" },
      h("p", { class: "cw-hint" },
        // 只列原文的紀錄目前沒有逐段引用（public_qa.citations 為空）：只標編號，出處統一看最後的「出處：」行
        c ? `來源 ${m[1]}：${sourceLabel(c, true)}` : `來源 ${m[1]}`,
        mostlyLatin(text) ? h("span", { class: "cw-lang-tag" }, "英文原文") : null),
      h("p", {}, text)));
  }
  return h("div", { class: "answer cw-answer" }, ...blocks,
    cites.length > 0 ? refToggle(`參考依據（${cites.length}）`,
      h("ol", { class: "cw-answer-sources" }, ...cites.map((c) => {
        const viewer = h("div");
        return h("li", { attrs: { value: String(c.number ?? "") } }, citationButton(c, viewer), viewer);
      }))) : null);
}

/** 畫面上有卡片的推薦方向：recommended 與 rec-v2 conditional 都顯示卡片（複製、回饋共用）。 */
function shownRecs(rec: Recommendation | null): RecItem[] {
  return rec && (rec.status === "recommended" || rec.status === "conditional") ? rec.recommendations : [];
}

/** 推薦結果：依狀態顯示推薦卡片、條件式方向、無結果的補問，或證據不足說明。 */
function renderRecommendation(rec: Recommendation): HTMLElement {
  const context = h("div", { class: "cw-rec-context" },
    rec.synthetic ? h("p", { class: "badge-line" }, h("span", { class: "badge warn" }, "SYNTHETIC"),
      "此結果使用合成測試候選，只驗證流程，不是正式推薦。") : null,
    rec.assumptions.length > 0 ? h("ul", { class: "plain cw-hint" }, ...rec.assumptions.map((a) => h("li", {}, a.text))) : null,
    rec.hard_conditions.length > 0
      ? h("p", { class: "cw-hint" }, h("span", { class: "badge warn" }, "已排除"), rec.hard_conditions.map((c) => c.label).join("、"))
      : null);
  const head = context.childElementCount > 0 ? context : null;
  if (rec.status === "no_match" && rec.reason === "undertone_condition_not_met") {
    // rec-v2/H06：只因來源的適用底調不符；不是排除條件，不提示放寬
    return h("div", { class: "rec-empty cw-notice-card" }, head,
      h("h4", {}, "沒有適用於你底調的來源方向"),
      h("p", { class: "cw-hint" }, "這只表示目前已核對的來源沒有涵蓋你的底調，不表示這些顏色不能用。"));
  }
  if (rec.status === "conditional") {
    // rec-v2：底調未知或不確定——依來源條件分開列出，並追問底調
    return h("div", { class: "cw-recs" }, head,
      h("p", { class: "cw-hint" }, "你的底調還沒確定，以下方向依來源的底調條件分開列出，不是依你的底調推薦。"),
      h("div", { class: "cw-alt-grid" }, ...rec.recommendations.map((r) => renderRecCard(r, rec.brief))),
      rec.question ? h("div", { class: "cw-clarify" }, h("h4", {}, "想再確認一下"), h("p", {}, rec.question),
        h("p", { class: "cw-hint" }, "直接在下方輸入框回答即可。")) : null);
  }
  if (rec.status === "no_match") {
    return h("div", { class: "rec-empty cw-notice-card" }, head,
      h("h4", {}, "沒有同時符合全部條件的方向"),
      h("p", {}, rec.question ?? "可以放寬其中一項條件再試；不放寬就不推薦。"),
      h("p", { class: "cw-hint" }, "系統不會自行放寬你說「不要」的條件。可以直接說「取消不要…」，或按上方「修改條件」調整。"));
  }
  if (rec.status === "insufficient_evidence") {
    return h("div", { class: "rec-empty cw-notice-card" }, head,
      h("h4", {}, "目前沒有可以引用來源的推薦方向"),
      h("p", { class: "cw-hint" }, "推薦只使用系統收錄、仍有效且你有權限查看的來源；沒有這樣的來源時寧可不推薦。"));
  }
  if (rec.status !== "recommended") return head ?? h("div");
  const [primary, ...alternatives] = rec.recommendations;
  return h("div", { class: "cw-recs" }, head,
    primary ? renderRecCard(primary, rec.brief) : null,
    alternatives.length > 0 ? h("div", { class: "cw-alt-grid" }, ...alternatives.map((r) => renderRecCard(r, rec.brief))) : null);
}

/** 推薦方向的角色名稱：rank 1 為主要推薦，其餘依序為替代方向 1、2；條件式方向顯示來源條件。 */
function roleTitle(r: RecItem): string {
  if (r.role === "conditional") return r.condition?.text ?? "條件式方向";
  return r.role === "primary" ? "主要推薦" : `替代方向 ${r.rank - 1}`;
}

/**
 * 單一推薦方向卡片。主要推薦是大卡（左側裝飾圖示區＋右側內容），替代方向是精簡卡。
 * 左側只放線條唇形圖示，不放照片或色票：後端沒有提供色值，畫面不自行編造顏色。
 * 理由的逐條主張與引用收在「查看來源」裡，點開才顯示（每次點引用仍向後端重新查原文）。
 */
function renderRecCard(r: RecItem, brief?: Brief | null): HTMLElement {
  const primary = r.role === "primary";
  const item = brief?.items.find((x) => x.rank === r.rank);
  if (brief) {
    // brief-v1：卡片只顯示「顏色＋為什麼」；理由標籤、說明、限制與逐條來源都收進「參考依據」
    const covered = brief.covered.includes(r.name);
    const ref = refToggle("參考依據",
      r.reason ? h("p", { class: "cw-hint" }, r.reason) : null,
      r.description ? h("p", { class: "cw-hint" }, r.description) : null,
      r.limitations.length > 0 ? h("ul", { class: "plain cw-hint" }, ...r.limitations.map((l) => h("li", {}, `限制：${l}`))) : null,
      h("ul", { class: "claims" }, ...r.claims.map(renderClaim)));
    const content = h("div", { class: "cw-rec-content" },
      h("p", { class: "cw-rec-role" }, r.role === "conditional" ? (item?.condition ?? roleTitle(r))
        : covered ? "適合" : primary ? "推薦" : "也可以考慮"),
      h("h4", { class: "cw-rec-name" }, r.name),
      item?.why ? h("p", { class: "cw-rec-reason" }, item.why) : null,
      ref);
    return h("article", { class: primary ? "rec-card primary cw-rec-primary" : "rec-card cw-rec-alt" },
      primary ? h("div", { class: "cw-rec-visual", attrs: { "aria-hidden": "true" } }, icon("lips", "cw-rec-visual-icon")) : null,
      content);
  }
  const claimsId = `claims-${Math.random().toString(36).slice(2, 10)}`;
  const claims = h("div", { class: "cw-claims", attrs: { id: claimsId } },
    h("ul", { class: "claims" }, ...r.claims.map(renderClaim)));
  claims.hidden = true;
  const citeCount = r.claims.reduce((n, c) => n + c.citations.length, 0);
  const toggle = h("button", {
    class: "cw-source-toggle",
    attrs: { type: "button", "aria-expanded": "false", "aria-controls": claimsId },
  }, h("span", {}, citeCount > 0 ? `查看來源（${citeCount}）` : "查看理由"), icon("arrowRight"));
  toggle.addEventListener("click", () => {
    const open = toggle.getAttribute("aria-expanded") !== "true";
    toggle.setAttribute("aria-expanded", String(open));
    claims.hidden = !open;
  });
  const content = h("div", { class: "cw-rec-content" },
    h("p", { class: "cw-rec-role" }, roleTitle(r)),
    h("h4", { class: "cw-rec-name" }, r.name),
    r.reason ? h("p", { class: "cw-rec-reason" }, r.reason) : null,
    r.description ? h("p", { class: "cw-rec-desc" }, r.description) : null,
    r.unmet.length > 0
      ? h("p", { class: "cw-hint" }, "未符合：", r.unmet.map((u) => `${FIELD_LABEL[u.field] ?? u.field}（${u.labels.join("、")}）`).join("；"))
      : null,
    r.tied_with.length > 0 ? h("p", { class: "cw-hint" }, "與其他方向同樣符合，順序只依固定編號，不代表較好。") : null,
    r.limitations.length > 0 ? h("ul", { class: "plain cw-hint" }, ...r.limitations.map((l) => h("li", {}, `限制：${l}`))) : null,
    r.claims.length > 0 ? toggle : null,
    claims);
  return h("article", { class: primary ? "rec-card primary cw-rec-primary" : "rec-card cw-rec-alt" },
    primary ? h("div", { class: "cw-rec-visual", attrs: { "aria-hidden": "true" } }, icon("lips", "cw-rec-visual-icon")) : null,
    content);
}

/** 一條主張：類型標籤＋文字＋每個引用的按鈕（展開後顯示原文與定位）。 */
function renderClaim(c: Claim): HTMLElement {
  const viewer = h("div");
  return h("li", {},
    h("span", { class: "badge" }, CLAIM_LABEL[c.claim_type] ?? c.claim_type), c.text,
    ...c.citations.map((cit) => citationButton(cit, viewer)),
    viewer);
}

/** 引用的頁碼／網頁定位文字。 */
function locatorText(c: { page_start?: number | null; page_end?: number | null; html_locator?: string | null }): string {
  if (c.page_start) {
    return c.page_end && c.page_end !== c.page_start ? `p.${c.page_start}–${c.page_end}` : `p.${c.page_start}`;
  }
  return c.html_locator ? `網頁段落 ${c.html_locator}` : "未標頁碼";
}

/**
 * 給人看的出處文字（citation-label-v1，與後端 source_label.label 同格式）：〈文章標題〉，刊物，年份，p.頁碼。
 * 書目沒有的欄位就省略；網頁文章沒有頁碼，也不顯示網頁段落定位；不顯示文件編號與版本。
 * withPage=false 用在「出處：」總覽行（同一篇文章只列一次）。
 */
function sourceLabel(c: { title?: string | null; venue?: string | null; year?: number | string | null;
  page_start?: number | null; page_end?: number | null }, withPage: boolean): string {
  const page = withPage && c.page_start ? locatorText(c) : null;
  const parts = [c.title ? `〈${c.title}〉` : "（未登錄標題的來源）", c.venue, c.year != null ? String(c.year) : null, page];
  return parts.filter((p): p is string => !!p).join("，");
}

const CITE_OFF_NOTE = "此來源目前無法查看（可能已停用、過期或不在你的查看範圍）。";

/** 引用的識別鍵（文件＋版本＋片段），用來比對畫面上的按鈕與後端最新的可用狀態。 */
function citeKey(c: { doc_key?: string; version_label?: string; chunk_key?: string }): string {
  return `${c.doc_key ?? ""}\u0000${c.version_label ?? ""}\u0000${c.chunk_key ?? ""}`;
}

/** 引用按鈕：可查看時點擊展開原文；不可查看時只顯示說明，不提供任何定位。 */
function citationButton(cit: Citation, viewer: HTMLElement): HTMLElement {
  if (!cit.available || !cit.doc_key || !cit.version_label || !cit.chunk_key) {
    return h("span", { class: "cite-off" }, `〔${cit.note ?? "來源目前無法查看"}〕`);
  }
  // S9：版本已更新但內容相同時，標示目前版本並註明原本引用的版本
  // 使用者 2026-10-25：按鈕不顯示網頁段落定位（body[1]>…）；2026-11-01：不顯示文件編號，
  // 改寫〈文章標題〉，刊物／學校／網站，年份，p.頁碼（PDF 才有頁碼）
  const label = `〔${sourceLabel(cit, true)}` +
    (cit.superseded && cit.cited_version_label ? `（原引用 ${cit.cited_version_label}）〕` : "〕");
  const btn = h("button", { class: "link cite", attrs: { type: "button", "aria-expanded": "false" } }, label);
  btn.dataset.cite = citeKey(cit);
  citeViewer.set(btn, viewer);
  btn.addEventListener("click", () => void toggleCitation(cit, btn, viewer));
  return btn;
}

// 引用按鈕 → 它展開原文用的區塊（同一條主張的引用共用一個區塊，區塊以 data-showing 記錄目前顯示哪一個）
const citeViewer = new WeakMap<HTMLElement, HTMLElement>();

/** 把引用按鈕換成「無法查看」說明；若原文正展開在畫面上，一併清掉。 */
function disableCitation(btn: HTMLElement): void {
  const viewer = citeViewer.get(btn);
  if (viewer && viewer.dataset.showing === btn.dataset.cite) {
    delete viewer.dataset.showing;
    replace(viewer);
  }
  btn.replaceWith(h("span", { class: "cite-off" }, `〔${CITE_OFF_NOTE}〕`));
}

// 畫面上的結果區塊 → request_id。回到分頁（visibilitychange／focus）時重新讀取，
// 把已不可用的引用就地關閉；不整塊重畫，避免使用者正在填的回饋表單被清掉。
const watched = new Map<HTMLElement, string>();
let rechecking = false;
let listening = false;

/** 登記要在回到分頁時重新檢查引用的結果區塊；監聽只註冊一次。 */
function watchCitations(box: HTMLElement, requestId: string): void {
  if (!listening) {
    listening = true;
    document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") void recheckCitations(); });
    window.addEventListener("focus", () => void recheckCitations());
  }
  watched.set(box, requestId);
}

/** 逐一重新讀取仍在畫面上的結果，關閉後端已判為不可用的引用（後端依角色與片段狀態判斷）。 */
async function recheckCitations(): Promise<void> {
  if (rechecking) return;
  rechecking = true;
  try {
    for (const [box, requestId] of [...watched]) {
      if (!box.isConnected) { watched.delete(box); continue; }
      const buttons = [...box.querySelectorAll<HTMLElement>("button.cite[data-cite]")];
      if (buttons.length === 0) continue;
      let view: RequestView;
      try {
        view = await api<RequestView>("GET", `/requests/${encodeURIComponent(requestId)}`);
      } catch {
        continue;  // 讀取失敗（離線等）就維持原狀，下次回到分頁再試；不因此誤判來源可用
      }
      if (view.masked) {
        // S9：整則已被後端遮蔽（依據失效或權限變更）→ 整塊換成遮蔽說明，原文與主張都不留在畫面上
        replace(box, renderRequestView(view));
        continue;
      }
      const available = [...(view.recommendation?.recommendations ?? [])
        .flatMap((r) => r.claims.flatMap((c) => c.citations)), ...(view.qa?.citations ?? [])]
        .filter((c) => c.available);
      // 版本已更新（superseded）的引用：畫面上的按鈕可能還是舊版本的鍵，舊鍵也算仍可用
      const ok = new Set([...available.map(citeKey), ...available.filter((c) => c.superseded)
        .map((c) => citeKey({ doc_key: c.doc_key ?? "", version_label: c.cited_version_label ?? "",
          chunk_key: c.cited_chunk_key ?? "" }))]);
      for (const btn of buttons) if (!ok.has(btn.dataset.cite ?? "")) disableCitation(btn);
    }
  } finally {
    rechecking = false;
  }
}
// 核心邏輯：只做「可用 → 不可用」的單向關閉；來源重新開放時要重新整理或重新點「查看結果」才會出現。

interface CitationText {
  title: string;
  venue: string | null;
  year: string | null;
  doc_key: string;
  version_label: string;
  page_start: number | null;
  page_end: number | null;
  html_locator: string | null;
  section: string | null;
  quote_text: string;
  citation: string | null;
}

/** 展開／收合引用原文；每次展開都向後端重新查詢（後端以角色與片段狀態判斷）。 */
async function toggleCitation(cit: Citation, btn: HTMLButtonElement, viewer: HTMLElement): Promise<void> {
  if (btn.getAttribute("aria-expanded") === "true") {
    btn.setAttribute("aria-expanded", "false");
    delete viewer.dataset.showing;
    replace(viewer);
    return;
  }
  btn.setAttribute("aria-expanded", "true");
  viewer.dataset.showing = btn.dataset.cite;
  replace(viewer, h("p", { class: "note", attrs: { role: "status" } }, "正在讀取來源…"));
  const params = new URLSearchParams({ doc: cit.doc_key ?? "", version: cit.version_label ?? "", chunk: cit.chunk_key ?? "" });
  try {
    const src = await api<CitationText>("GET", `/sources/citation?${params}`);
    replace(viewer, h("div", { class: "cite-box" },
      h("p", { class: "conv-meta" },
        // 使用者 2026-10-25：不顯示網頁段落定位與章節標題；PDF 保留頁碼
        sourceLabel(src, true)),
      h("blockquote", { class: "quote" }, src.quote_text),
      src.citation ? h("p", { class: "citation" }, src.citation) : null,
      h("p", { class: "note" }, "以上是來源原文；回答只引用其中有依據的內容。")));
  } catch (err) {
    const gone = err instanceof ApiError && err.status === 404;
    if (gone) {
      disableCitation(btn);  // 後端判為不可用：按鈕改成說明，之後不再提供定位
      return;
    }
    replace(viewer, notice("error", describeError(err)));
  }
}

/** 補問（最多 2 題）；需先確認的題目會擋住推薦。 */
function renderClarification(view: RequestView): HTMLElement | null {
  if (view.clarification.length === 0) return null;
  return h("div", { class: "cw-clarify" }, h("h4", {}, "想再確認一下"),
    h("ol", {}, ...view.clarification.map((q) => h("li", {}, q.question,
      q.blocking ? h("span", { class: "badge warn" }, "需先確認") : null))),
    h("p", { class: "cw-hint" }, "直接在下方輸入框回答即可。"));
}

/** 這次請求更新了哪些條件；沒有變更時不顯示，避免每則回覆都多一段雜訊。 */
function renderChanges(view: RequestView): HTMLElement | null {
  const changes = view.applied_changes;
  if (changes.length === 0) return null;
  return h("div", { class: "cw-changes" }, h("h4", {}, "這次更新的條件"),
    h("ul", {}, ...changes.map((c) =>
      h("li", {}, `${c.field_label ?? c.field}｜${OP_LABEL[c.op] ?? c.op}：${c.label}${c.raw ? `（「${c.raw}」）` : ""}`))));
}

/** 整體限制（商品未啟用、過敏不保證安全、SYNTHETIC 等）；一律顯示，不收合。 */
function renderLimitations(limits: string[]): HTMLElement | null {
  if (limits.length === 0) return null;
  // brief-v1：過敏相關一律顯示（規範 6）；其他限制收進「注意事項」小按鈕
  const shown = limits.filter((l) => l.includes("過敏"));
  const rest = limits.filter((l) => !l.includes("過敏"));
  return h("div", { class: "cw-limits" },
    shown.length > 0 ? h("ul", {}, ...shown.map((l) => h("li", {}, l))) : null,
    rest.length > 0 ? refToggle(`注意事項（${rest.length}）`, h("ul", {}, ...rest.map((l) => h("li", {}, l)))) : null);
}

/**
 * 回覆底部：低視覺權重的細線操作（有幫助、沒幫助、複製）＋時間與 request_id，
 * 以及按下有幫助／沒幫助後展開的回饋表單（針對哪個方向、原因、補充說明）。
 * 回饋以 request_id 送出；後端確認是自己的、已完成的請求，且方向屬於這次結果。
 * 請求仍在處理中（started）時不提供回饋。
 */
function renderFeedback(view: RequestView, onFeedback?: () => void): HTMLElement {
  const meta = h("p", { class: "cw-meta" }, `${formatClock(view.started_at)} · `,
    h("span", { class: "mono" }, `request_id ${view.request_id}`));
  const copyBtn = actionButton("copy", "複製回覆");
  copyBtn.addEventListener("click", () => void copyReply());
  if (view.request_status === "started") {
    return h("footer", { class: "cw-ai-foot" }, h("div", { class: "cw-actions" }, copyBtn), meta);
  }

  const prev = view.feedback;
  const recs = shownRecs(view.recommendation);
  const name = `fb-${view.request_id}`;
  let rating: 1 | -1 | null = prev?.rating ?? null;
  const msg = h("div", { attrs: { "aria-live": "polite" } });
  const upBtn = actionButton("thumbUp", "有幫助");
  const downBtn = actionButton("thumbDown", "沒幫助");

  const rank = h("select", { attrs: { id: `${name}-rank` } },
    h("option", { attrs: { value: "" } }, "整體結果"),
    ...recs.map((r) => {
      const o = h("option", { attrs: { value: String(r.rank) } }, `${roleTitle(r)}：${r.name}`);
      o.selected = prev?.rank === r.rank;
      return o;
    }));
  const reasons = h("div", { class: "cw-choice-row" }, ...Object.entries(view.feedback_reasons).map(([code, label]) => {
    const box = h("input", { attrs: { type: "checkbox", name: `${name}-reason`, value: code } });
    box.checked = prev?.reason_codes.includes(code) ?? false;
    return h("label", { class: "cw-choice" }, box, h("span", {}, label));
  }));
  const comment = h("textarea", { attrs: { id: `${name}-comment`, maxlength: "2000", rows: "2", placeholder: "想補充的地方（選填）" } });
  comment.value = prev?.comment ?? "";
  const submit = h("button", { class: "cw-btn-primary cw-btn-sm", attrs: { type: "submit" } }, prev ? "更新回饋" : "送出回饋");
  const heading = h("p", { class: "cw-feedback-title" });

  const form = h("form", { class: "feedback cw-feedback", on: { submit: (ev) => { ev.preventDefault(); void send(); } } },
    heading,
    recs.length > 0 ? h("div", { class: "cw-field" }, h("label", { attrs: { for: `${name}-rank` } }, "針對哪一個方向"), rank) : null,
    h("fieldset", { class: "cw-field cw-choices" }, h("legend", {}, "原因（可複選）"), reasons),
    h("div", { class: "cw-field" }, h("label", { attrs: { for: `${name}-comment` } }, "補充說明"), comment),
    h("p", { class: "cw-hint" }, `回饋會連到這次的 request_id（${view.request_id}），只有你自己看得到。`),
    h("div", { class: "cw-form-actions" }, submit));
  form.hidden = true;

  upBtn.addEventListener("click", () => choose(1));
  downBtn.addEventListener("click", () => choose(-1));
  syncButtons();
  if (prev) msg.appendChild(h("p", { class: "cw-hint" }, `已收到你的回饋（${formatTime(prev.updated_at)}）。`));

  return h("footer", { class: "cw-ai-foot" },
    h("div", { class: "cw-actions", attrs: { role: "group", "aria-label": "這則回覆的操作" } }, upBtn, downBtn, copyBtn),
    meta, msg, form);

  // 按下有幫助／沒幫助：記住選擇並展開回饋表單；再按一次同一顆則收合表單（不會刪除已送出的回饋）
  function choose(value: 1 | -1): void {
    const sameOpen = rating === value && !form.hidden;
    rating = value;
    syncButtons();
    form.hidden = sameOpen;
    heading.textContent = value === 1 ? "謝謝！想補充哪裡有幫助嗎？（選填）" : "哪裡不符合需求？（選填）";
    if (!form.hidden) comment.focus({ preventScroll: true });
  }

  // 依目前選擇更新兩顆按鈕的按下狀態（aria-pressed 供讀屏軟體與樣式使用）
  function syncButtons(): void {
    upBtn.setAttribute("aria-pressed", String(rating === 1));
    downBtn.setAttribute("aria-pressed", String(rating === -1));
  }

  // 送出回饋；必填「有幫助／沒幫助」（由上方按鈕決定），其他欄位選填
  async function send(): Promise<void> {
    if (rating === null) {
      replace(msg, notice("error", "請先選擇「有幫助」或「沒幫助」。"));
      return;
    }
    const body = {
      rating,
      reason_codes: [...form.querySelectorAll<HTMLInputElement>(`input[name="${name}-reason"]:checked`)].map((b) => b.value),
      comment: comment.value.trim() || null,
      ...(rank.value ? { rank: Number(rank.value) } : {}),
    };
    submit.disabled = true;
    replace(msg, h("p", { class: "cw-hint", attrs: { role: "status" } }, "送出中…"));
    try {
      const saved = await api<FeedbackSaved>("PUT", `/requests/${encodeURIComponent(view.request_id)}/feedback`, body);
      replace(msg, notice("ok", `已收到你的回饋（${formatTime(saved.updated_at)}）。`));
      submit.textContent = "更新回饋";
      form.hidden = true;
      onFeedback?.();
    } catch (err) {
      replace(msg, notice("error", `回饋沒有送出：${describeError(err)}`));
    } finally {
      submit.disabled = false;
    }
  }

  // 複製回覆文字到剪貼簿；瀏覽器不允許時明確告知，不靜默失敗
  async function copyReply(): Promise<void> {
    try {
      await navigator.clipboard.writeText(copyText(view));
      copyBtn.replaceChildren(icon("check"));
      copyBtn.setAttribute("aria-label", "已複製");
      copyBtn.title = "已複製";
      window.setTimeout(() => {
        copyBtn.replaceChildren(icon("copy"));
        copyBtn.setAttribute("aria-label", "複製回覆");
        copyBtn.title = "複製回覆";
      }, 1600);
    } catch {
      replace(msg, notice("error", "無法複製：瀏覽器沒有允許存取剪貼簿。"));
    }
  }
}

/** 回覆底部的小型細線圖示按鈕；名稱放在 aria-label 與滑鼠提示。 */
function actionButton(name: "thumbUp" | "thumbDown" | "copy", label: string): HTMLButtonElement {
  return h("button", { class: "cw-action", attrs: { type: "button", "aria-label": label, title: label } }, icon(name));
}

/** 訊息時間：今天只顯示「上午 10:24」，其他日期加上「5月18日」。 */
export function formatClock(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const time = d.toLocaleTimeString("zh-TW", { hour: "numeric", minute: "2-digit" });
  const now = new Date();
  if (d.toDateString() === now.toDateString()) return time;
  const date = d.getFullYear() === now.getFullYear()
    ? `${d.getMonth() + 1}月${d.getDate()}日`
    : `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日`;
  return `${date} ${time}`;
}
