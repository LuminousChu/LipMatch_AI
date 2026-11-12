// 聊天工作區（#/chat、#/chat/{對話 id}）：2026-10-20 依「日常清新」視覺改版。
//
// 版面：左側對話紀錄側欄（品牌、開始新對話、對話清單、其他頁面連結）＋右側聊天區
//      （頂部標題／條件標籤／帳戶選單 → 訊息串 → 底部懸浮輸入列）。手機版側欄改為抽屜。
// 功能沿用舊版對話頁，API 不變：
//   - 對話清單、建立對話：GET／POST /api/conversations
//   - 對話內容與需求紀錄：GET /api/conversations/{id}；每則需求的結果由 result.ts 以 request_id 讀回
//   - 送出需求或修改條件：POST /api/conversations/{id}/messages（Enter 送出、Shift+Enter 換行、選字中不送出）
//   - 偏好修改（需求摘要＋表單）：chat-settings.ts 的對話框，PATCH /api/conversations/{id}
//   - 對話改名：PATCH /api/conversations/{id}（只帶 title）
// 偏好的解析、合併、衝突判斷、推薦與引用權限都在後端；前端只顯示結果，不自行推定任何條件，
// 也不放示範照片、色票或假回答（2026-11-07 起：回答都由模型依收錄來源整理、每句附出處；route-v4 已不出規則推薦卡）。

import { api, ApiError, describeError, MESSAGE_TIMEOUT_MS } from "../api.js";
import { h, notice, replace } from "../dom.js";
import { icon, leafLogo, type IconName } from "../icons.js";
import { currentUser, logout, type User } from "../session.js";
import { openSettingsDialog, pendingItems } from "./chat-settings.js";
import {
  type Conversation, type ConversationDetail, type RequestRow, type Summary, SCENARIO_LABEL,
} from "./chat-types.js";
import { formatClock, OUTCOME_LABEL, showRequestResult } from "./result.js";

/** 送出需求的回應：只取 request_id，完整結果改由 GET /api/requests/{id} 讀回（與重新整理後一致）。 */
interface MessageResult {
  request_id: string;
}

/** progress-hint-v1：GET /api/progress/{權杖} 的回應（後端 app/progress.py view）。 */
interface ProgressView {
  stage: string;
  label: string;
  sources: string[];
  elapsed_s: number;
}

/** 開啟對話時自動讀回結果的最近需求數；更早的需求只顯示摘要，按「顯示這次結果」才讀取。 */
const AUTO_LOAD_RECENT = 6;

/** 頂部最多顯示幾個條件標籤；其餘以「＋N」表示，完整內容在「修改條件」裡。 */
const MAX_CHIPS = 5;

/** 空白對話的範例說法：點了只會填進輸入框，不會自動送出，也不是預設條件。 */
// 範例問句（2026-11-07）
const EXAMPLES = ["想找日常用的自然感唇彩，不要霧面的", "約會想要溫柔、有氣色感的唇彩", "我膚色偏黃，適合擦什麼顏色的唇彩？"];

/** 條件標籤直接顯示值的欄位（其餘欄位顯示「項目：值」）。 */
const BARE_CHIP_FIELDS = new Set(["occasion", "effects", "liked_colors", "liked_finishes"]);

/** 視為「已有條件」的摘要來源狀態；未知、未回答、預設不顯示成標籤。 */
const SHOWN_STATUS = new Set(["user", "tentative", "none_known", "has_known", "unsure"]);

/**
 * 在沒有選取對話時輸入的第一句話：先建立對話、切換網址，切換後的畫面再把這句送出。
 * 只存在記憶體，重新整理就消失，不寫入任何儲存空間。
 */
let pendingQuestion: { conversationId: string; text: string } | null = null;

/** 側欄底部的其他頁面連結（角色只看後端 /api/me；後端仍會擋下未授權請求）。 */
function otherPages(user: User): [string, string, IconName][] {
  const admin: [string, string, IconName][] = user.role === "admin"
    ? [["#/review", "審核", "review"], ["#/monitor", "監控", "monitor"]] : [];
  return [["#/sources", "來源", "sources"], ...admin, ["#/status", "連線狀態", "status"]];
}

/** 側欄日期：今天、昨天，其餘顯示「5月18日」（跨年加年份）。 */
function formatDay(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const now = new Date();
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (d.toDateString() === now.toDateString()) return "今天";
  if (d.toDateString() === yesterday.toDateString()) return "昨天";
  return d.getFullYear() === now.getFullYear()
    ? `${d.getMonth() + 1}月${d.getDate()}日`
    : `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日`;
}

/**
 * 需求摘要 → 頂部條件標籤文字。只取使用者已提供的欄位；排除條件拆成「不要X」，
 * 其他欄位維持後端的顯示名稱，不在前端改寫意思。
 */
function conditionChips(summary: Summary): { text: string; hard: boolean }[] {
  const chips: { text: string; hard: boolean }[] = [];
  for (const r of summary.rows) {
    if (!SHOWN_STATUS.has(r.status) || r.field === "notes") continue;
    if (r.hard) {
      for (const v of r.value.split("、")) chips.push({ text: `不要${v}`, hard: true });
    } else {
      chips.push({ text: BARE_CHIP_FIELDS.has(r.field) ? r.value : `${r.label}：${r.value}`, hard: false });
    }
  }
  return chips;
}

/**
 * 對話頁進入點：建立工作區外框後，載入側欄清單與選取的對話。
 * @param view 頁面容器（app.ts 的 #view）
 * @param selectedId 網址中選取的對話 id（沒有時為 null，顯示新對話的空白畫面）
 */
export async function renderChat(view: HTMLElement, selectedId: string | null): Promise<void> {
  const user = currentUser();
  if (user === null) return;  // app.ts 只在已登入時呼叫；這裡只是防呆

  // ---- 外框：側欄＋聊天區 ----
  const shell = h("div", { class: "cw" });
  const convList = h("nav", { class: "cw-conv-list", attrs: { "aria-label": "我的對話" } });
  const newBtn = h("button", { class: "cw-new", attrs: { type: "button" }, on: { click: () => void createConversation() } },
    icon("plus"), h("span", {}, "開始新對話"));
  const closeSideBtn = h("button", { class: "cw-icon-btn cw-side-close", attrs: { type: "button", "aria-label": "關閉對話列表" } },
    icon("close"));
  const side = h("aside", { class: "cw-side", attrs: { id: "cw-side", "aria-label": "對話紀錄" } },
    h("div", { class: "cw-brand" }, leafLogo(), h("span", { class: "cw-brand-name" }, "LipMatch"),
      h("span", { class: "cw-brand-ai" }, "AI"), closeSideBtn),
    newBtn, convList,
    h("nav", { class: "cw-side-foot", attrs: { "aria-label": "其他頁面" } },
      ...otherPages(user).map(([href, label, name]) => h("a", { class: "cw-side-link", attrs: { href } }, icon(name), label))));
  const backdrop = h("div", { class: "cw-backdrop", attrs: { "aria-hidden": "true" } });

  const menuBtn = h("button", {
    class: "cw-icon-btn cw-menu-btn",
    attrs: { type: "button", "aria-label": "開啟對話列表", "aria-controls": "cw-side", "aria-expanded": "false" },
  }, icon("menu"));
  const headText = h("div", { class: "cw-head-text" });
  const head = h("header", { class: "cw-head" }, menuBtn, headText, buildAccount(user));
  const threadInner = h("div", { class: "cw-thread-inner" });
  const thread = h("div", { class: "cw-thread", attrs: { "aria-label": "對話內容" } }, threadInner);
  const composer = buildComposer((text) => send(text));
  const main = h("section", { class: "cw-main" }, head, thread, composer.form);
  replace(view, shell);
  shell.append(side, backdrop, main);

  // ---- 手機版側欄抽屜 ----
  // 開關都只改 class 與 aria-expanded；點背景、按 Esc 或選了對話（網址改變會重畫整頁）就關閉
  function setSide(open: boolean): void {
    shell.classList.toggle("cw-side-open", open);
    menuBtn.setAttribute("aria-expanded", String(open));
    if (open) newBtn.focus();
  }
  menuBtn.addEventListener("click", () => setSide(true));
  closeSideBtn.addEventListener("click", () => setSide(false));
  backdrop.addEventListener("click", () => setSide(false));
  shell.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && shell.classList.contains("cw-side-open")) setSide(false);
  });

  // ---- 側欄清單：送出需求或改名後重新載入，名稱與時間才會同步 ----
  async function loadList(): Promise<void> {
    let conversations: Conversation[];
    try {
      conversations = (await api<{ items: Conversation[] }>("GET", "/conversations")).items;
    } catch (err) {
      replace(convList, notice("error", describeError(err)));
      return;
    }
    replace(convList, ...conversations.map((c) => {
      const active = c.id === selectedId;
      const title = c.title || "未命名對話";
      // 刪除鍵放在連結外面（按鈕不能包在 <a> 裡），滑過或鍵盤聚焦該列時才明顯顯示
      const del = h("button", {
        class: "cw-icon-btn cw-conv-del",
        attrs: { type: "button", "aria-label": `刪除對話「${title}」`, title: "刪除對話" },
        on: { click: () => void deleteConversation(c.id, title, del) },
      }, icon("trash"));
      return h("div", { class: active ? "cw-conv-row active" : "cw-conv-row" },
        h("a", {
          class: active ? "cw-conv active" : "cw-conv",
          attrs: { href: `#/chat/${c.id}`, ...(active ? { "aria-current": "page" } : {}) },
        }, icon("chat"),
        h("span", { class: "cw-conv-text" },
          h("span", { class: "cw-conv-title" }, title),
          h("span", { class: "cw-conv-meta" },
            formatDay(c.updated_at),
            c.scenario_source === "user" ? ` · ${SCENARIO_LABEL[c.scenario]}` : ""))),
        del);
    }));
    if (conversations.length === 0) convList.appendChild(h("p", { class: "cw-empty-list" }, "還沒有對話。"));
  }

  // ---- 刪除對話（2026-11-07）：後端只隱藏、不刪資料；刪的是目前開著的對話就回到新對話畫面 ----
  async function deleteConversation(id: string, title: string, btn: HTMLButtonElement): Promise<void> {
    if (!confirm(`刪除對話「${title}」？\n刪除後不會再出現在列表，也不能再打開。`)) return;
    btn.disabled = true;
    try {
      await api("DELETE", `/conversations/${encodeURIComponent(id)}`);
      if (id === selectedId) location.hash = "#/chat";
      else await loadList();
    } catch (err) {
      btn.disabled = false;
      convList.prepend(notice("error", `刪除失敗：${describeError(err)}`));
    }
  }

  // ---- 建立新對話後切換過去（新對話的條件一律從空白開始，不沿用其他對話） ----
  async function createConversation(): Promise<void> {
    newBtn.disabled = true;
    try {
      const c = await api<Conversation>("POST", "/conversations", {});
      location.hash = `#/chat/${c.id}`;
    } catch (err) {
      threadInner.prepend(notice("error", describeError(err)));
      newBtn.disabled = false;
    }
  }

  // ---- 目前開啟的對話 ----
  let conv: ConversationDetail | null = null;

  // 頂部：標題（可改名）、條件標籤、修改條件
  function renderHead(): void {
    if (conv === null) {
      replace(headText, h("div", { class: "cw-title-row" }, h("h2", { class: "cw-title" }, "新對話")),
        h("p", { class: "cw-head-sub" }, "說出需求後，系統會記下條件並給你有來源的色系方向。"));
      return;
    }
    const current = conv;
    const editBtn = h("button", { class: "cw-icon-btn cw-icon-btn-sm", attrs: { type: "button", "aria-label": "重新命名對話" } }, icon("edit"));
    const titleRow = h("div", { class: "cw-title-row" },
      h("h2", { class: "cw-title" }, current.title || "未命名對話"), editBtn);
    editBtn.addEventListener("click", () => startRename(titleRow, current));

    const chips = conditionChips(current.summary);
    const pending = pendingItems(current.summary);
    const settingsBtn = h("button", { class: "cw-chip-action", attrs: { type: "button" } }, icon("sliders"), "修改條件");
    settingsBtn.addEventListener("click", () => openSettings());
    const chipRow = h("div", { class: "cw-chips", attrs: { role: "group", "aria-label": "目前條件" } },
      ...chips.slice(0, MAX_CHIPS).map((c) => h("span", { class: c.hard ? "cw-chip cw-chip-hard" : "cw-chip" }, c.text)),
      chips.length > MAX_CHIPS ? h("span", { class: "cw-chip cw-chip-more" }, `＋${chips.length - MAX_CHIPS}`) : null,
      chips.length === 0 ? h("span", { class: "cw-chip-empty" }, "尚未設定條件") : null,
      pending.length > 0
        ? h("button", { class: "cw-chip cw-chip-warn", attrs: { type: "button" }, on: { click: () => openSettings() } },
          `${pending.length} 項待確認`)
        : null,
      settingsBtn);
    replace(headText, titleRow, chipRow);
  }

  // 標題改成輸入框：Enter 或離開輸入框時儲存、Esc 取消；空白代表清除名稱
  function startRename(titleRow: HTMLElement, current: ConversationDetail): void {
    const input = h("input", {
      class: "cw-title-input",
      attrs: { maxlength: "200", value: current.title ?? "", "aria-label": "對話名稱" },
    });
    let done = false;
    const finish = async (save: boolean): Promise<void> => {
      if (done) return;
      done = true;
      const title = input.value.trim() || null;
      if (save && title !== current.title) {
        try {
          await api("PATCH", `/conversations/${encodeURIComponent(current.id)}`, { title });
          current.title = title;
          void loadList();
        } catch (err) {
          threadInner.prepend(notice("error", `名稱沒有儲存：${describeError(err)}`));
        }
      }
      renderHead();
    };
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && !ev.isComposing && ev.keyCode !== 229) { ev.preventDefault(); void finish(true); }
      if (ev.key === "Escape") { ev.preventDefault(); void finish(false); }
    });
    input.addEventListener("blur", () => void finish(true));
    replace(titleRow, input);
    input.focus();
    input.select();
  }

  // 開啟「修改條件」對話框；儲存後同步頂部標籤與側欄
  function openSettings(): void {
    if (conv === null) return;
    openSettingsDialog(shell, conv, (fresh) => {
      conv = fresh;
      renderHead();
      void loadList();
    });
  }

  // 重新讀取目前對話（送出需求後條件可能改變），只更新頂部，不重畫訊息串
  async function refreshConversation(): Promise<void> {
    if (conv === null) return;
    try {
      conv = await api<ConversationDetail>("GET", `/conversations/${encodeURIComponent(conv.id)}`);
      renderHead();
    } catch {
      // 頂部更新失敗不影響已顯示的回覆；下次開啟對話會重新讀取
    }
  }

  // ---- 訊息串 ----
  // 使用者訊息：靠右的淡綠泡泡＋時間（iso 為 null 代表還在傳送）
  function userMessage(text: string, iso: string | null): HTMLElement {
    return h("div", { class: "cw-msg cw-msg-user" },
      h("div", { class: "cw-msg-col" },
        h("p", { class: "cw-bubble cw-bubble-user" }, text),
        h("p", { class: "cw-meta" }, iso ? formatClock(iso) : "傳送中…")),
      h("span", { class: "cw-avatar cw-avatar-user", attrs: { "aria-hidden": "true" } }, icon("user")));
  }

  // AI 回覆的外框：左側葉片頭像＋內容區（內容由 result.ts 畫入）
  function aiMessage(): { wrap: HTMLElement; body: HTMLElement } {
    const body = h("div", { class: "cw-msg-col cw-ai-body" });
    const wrap = h("div", { class: "cw-msg cw-msg-ai" },
      h("span", { class: "cw-avatar cw-avatar-ai", attrs: { "aria-hidden": "true" } }, leafLogo()), body);
    return { wrap, body };
  }

  // 處理中的提示（三個點＋說明文字），讀屏軟體會讀到 role=status 的文字
  function typing(text: string): HTMLElement {
    return h("div", { class: "cw-typing", attrs: { role: "status", "aria-busy": "true" } },
      h("span", { class: "cw-dots", attrs: { "aria-hidden": "true" } }, h("i"), h("i"), h("i")), text);
  }

  // 一次需求＝使用者訊息＋AI 回覆；autoload 為 false 時只放摘要，按鈕按了才讀結果
  function appendTurn(row: RequestRow, autoload: boolean): Promise<void> {
    const ai = aiMessage();
    threadInner.append(userMessage(row.question, row.started_at), ai.wrap);
    if (autoload) return showRequestResult(ai.body, row.request_id);
    const label = row.answer_masked
      ? "回答已隱藏（依據已更新或受限）"
      : OUTCOME_LABEL[row.outcome ?? ""] ?? (row.answer ? "已回答" : "未產生回答");
    const loadBtn = h("button", { class: "cw-link-btn", attrs: { type: "button" } }, "顯示這次結果", icon("arrowRight"));
    loadBtn.addEventListener("click", () => void showRequestResult(ai.body, row.request_id));
    replace(ai.body, h("p", { class: "cw-status-tag", data: { kind: "info" } }, label),
      h("p", { class: "cw-meta" }, `${formatClock(row.started_at)} · `, h("span", { class: "mono" }, `request_id ${row.request_id}`)),
      loadBtn);
    return Promise.resolve();
  }

  // 沒有任何需求時的初始畫面：品牌標誌、引導文字、範例說法、來源說明
  function welcome(): HTMLElement {
    return h("div", { class: "cw-welcome" },
      h("div", { class: "cw-welcome-mark" }, leafLogo()),
      h("h3", {}, "今天想找什麼樣的唇色？"),
      h("p", { class: "cw-welcome-sub" }, "說說場合、喜歡或不想要的妝感，我會依收錄的來源，給你有依據的色系方向。"),
      h("div", { class: "cw-examples", attrs: { role: "group", "aria-label": "範例說法（點選後填入輸入框）" } },
        ...EXAMPLES.map((text) => h("button", {
          class: "cw-example", attrs: { type: "button" },
          on: { click: () => composer.fill(text) },
        }, text))),
      conv !== null
        ? h("button", { class: "cw-link-btn", attrs: { type: "button" }, on: { click: () => openSettings() } },
          icon("sliders"), "也可以先設定條件")
        : null,
      h("p", { class: "cw-welcome-note" }, icon("info"),
        // welcome-v2（2026-11-07）：舊說明寫「經人工核對」「規則版推薦」「模型尚未決定」都已過時
        h("span", {}, "諮詢內容皆根據文獻來源；資訊不足時會再次詢問，提供最適合您的專業諮詢。" +
          "目前不進行任何商業推薦，僅提供諮詢；業務範圍外的問題，恕無法回覆。")));
  }

  // 捲到訊息串最下方（新訊息或讀完最近結果後）
  function scrollToEnd(): void {
    thread.scrollTop = thread.scrollHeight;
  }

  /**
   * progress-hint-v1（2026-11-08）：等待回答時每秒向後端查進度（GET /api/progress/{權杖}），
   * 在等待框下方顯示「現在做到哪一步」與這次參考的來源標題。查不到（404、網路錯誤）就只顯示原本的等待文字，
   * 不影響送出與回答。回傳停止查詢的函式。內容一律用 textContent 顯示（h() 建元素），不插 HTML。
   */
  function pollProgress(token: string, box: HTMLElement): () => void {
    let stopped = false;
    let busy = false;
    const timer = window.setInterval(() => {
      if (stopped || busy) return;
      busy = true;
      api<ProgressView>("GET", `/progress/${encodeURIComponent(token)}`, undefined, 5000)
        .then((p) => {
          if (stopped) return;
          const items = (p.sources ?? []).map((t) => h("li", {}, `〈${t}〉`));
          replace(box, h("p", { class: "cw-progress-stage" }, p.label),
            items.length > 0
              ? h("div", {}, h("p", { class: "cw-progress-hint" }, `正在參考 ${items.length} 篇來源：`),
                h("ul", { class: "cw-progress-sources" }, ...items))
              : null);
        })
        .catch(() => { /* 查不到進度不影響回答，下一秒再試 */ })
        .finally(() => { busy = false; });
    }, 1000);
    return () => { stopped = true; window.clearInterval(timer); };
  }

  // 送出需求：沒有選取對話時先建立對話再切換；有對話則立即顯示訊息並等待後端結果
  async function send(text: string): Promise<boolean> {
    if (conv === null) {
      try {
        const c = await api<Conversation>("POST", "/conversations", {});
        pendingQuestion = { conversationId: c.id, text };
        location.hash = `#/chat/${c.id}`;
        return true;
      } catch (err) {
        threadInner.append(notice("error", `需求沒有送出：${describeError(err)}`));
        return false;
      }
    }
    threadInner.querySelector(".cw-welcome")?.remove();
    const mine = userMessage(text, null);
    const mineMeta = mine.querySelector<HTMLElement>(".cw-meta");
    const ai = aiMessage();
    // 等待框只放固定文字（2026-11-08：不顯示「通常需要 20～40 秒」與已等秒數）；
    // 進度與來源由下方 progressBox 顯示。前端逾時仍是 MESSAGE_TIMEOUT_MS（2026-11-06 修正）。
    const waiting = typing("正在整理需求並查找有來源的推薦…");
    // progress-hint-v1：等待框下方放進度區；權杖用瀏覽器亂數產生，只用來查這一則的進度
    const progressBox = h("div", { class: "cw-progress", attrs: { "aria-live": "polite" } });
    const progressToken = typeof crypto.randomUUID === "function" ? crypto.randomUUID() : null;
    const stopProgress = progressToken ? pollProgress(progressToken, progressBox) : () => undefined;
    replace(ai.body, waiting, progressBox);
    threadInner.append(mine, ai.wrap);
    scrollToEnd();
    try {
      const res = await api<MessageResult>("POST", `/conversations/${encodeURIComponent(conv.id)}/messages`, { question: text },
        MESSAGE_TIMEOUT_MS, progressToken ? { "X-Progress-Token": progressToken } : {});
      stopProgress();
      if (mineMeta) mineMeta.textContent = formatClock(new Date().toISOString());
      await showRequestResult(ai.body, res.request_id);
      scrollToEnd();
      void refreshConversation();
      void loadList();
      return true;
    } catch (err) {
      // 等太久（前端逾時或 nginx 504）：問題已送到後端，後端會照常做完並存檔，所以不說「沒有送出」
      if (err instanceof ApiError && (err.code === "timeout" || err.status === 504)) {
        if (mineMeta) mineMeta.textContent = "已送出";
        replace(ai.body, notice("info", "這題花的時間比平常久，系統還在整理回答。請過一兩分鐘重新整理這個對話查看結果。"));
        void loadList();
        return true;
      }
      mine.classList.add("cw-msg-failed");
      if (mineMeta) mineMeta.textContent = "未送出";
      replace(ai.body, notice("error", `需求沒有送出：${describeError(err)}`));
      return false;
    } finally {
      stopProgress();
    }
  }

  // ---- 載入：側欄與對話同時讀取 ----
  const listLoading = loadList();
  if (selectedId === null) {
    renderHead();
    threadInner.append(welcome());
    await listLoading;
    composer.focus();
    return;
  }
  replace(threadInner, typing("正在讀取對話…"));
  try {
    conv = await api<ConversationDetail>("GET", `/conversations/${encodeURIComponent(selectedId)}`);
  } catch (err) {
    renderHead();
    replace(threadInner, notice("error", describeError(err)));
    composer.disable();
    await listLoading;
    return;
  }
  renderHead();
  replace(threadInner);
  const requests = conv.requests;
  if (requests.length === 0) threadInner.append(welcome());
  const loads = requests.map((r, i) => appendTurn(r, i >= requests.length - AUTO_LOAD_RECENT));
  scrollToEnd();
  await Promise.all([...loads, listLoading]);
  scrollToEnd();

  // 從空白畫面輸入的第一句：對話建立並切換後在這裡送出
  if (pendingQuestion !== null && pendingQuestion.conversationId === conv.id) {
    const text = pendingQuestion.text;
    pendingQuestion = null;
    await composer.submit(text);
  }
  composer.focus();
}

/**
 * 頂部右側的帳戶按鈕與選單（使用者名稱、角色、登出）。
 * 選單以 hidden 切換；焦點離開選單、按 Esc 都會關閉。
 */
function buildAccount(user: User): HTMLElement {
  const btn = h("button", {
    class: "cw-avatar-btn",
    attrs: {
      type: "button", "aria-label": `帳戶：${user.username}`, "aria-haspopup": "true",
      "aria-expanded": "false", "aria-controls": "cw-account-menu",
    },
  }, icon("user"));
  const logoutBtn = h("button", { class: "cw-menu-item", attrs: { type: "button" } }, icon("logout"), "登出");
  const menu = h("div", { class: "cw-menu", attrs: { id: "cw-account-menu" } },
    h("p", { class: "cw-menu-name" }, user.username),
    h("p", { class: "cw-menu-role" }, user.role === "admin" ? "管理者" : "使用者"),
    logoutBtn);
  menu.hidden = true;
  const wrap = h("div", { class: "cw-account" }, btn, menu);

  // 開關選單並同步 aria-expanded
  const setOpen = (open: boolean): void => {
    menu.hidden = !open;
    btn.setAttribute("aria-expanded", String(open));
  };
  btn.addEventListener("click", () => setOpen(menu.hidden));
  wrap.addEventListener("keydown", (ev) => {
    if ((ev as KeyboardEvent).key === "Escape" && !menu.hidden) { setOpen(false); btn.focus(); }
  });
  // 焦點移到選單外就關閉（focusout 的 relatedTarget 是下一個取得焦點的元素）
  wrap.addEventListener("focusout", (ev) => {
    const next = (ev as FocusEvent).relatedTarget;
    if (!(next instanceof Node) || !wrap.contains(next)) setOpen(false);
  });
  logoutBtn.addEventListener("click", () => {
    void logout().catch(() => undefined).then(() => { location.hash = "#/login"; });
  });
  return wrap;
}

/**
 * 底部懸浮輸入列：可自動長高的輸入框＋圓形送出鈕。
 * Enter 送出、Shift+Enter 換行；中文輸入法選字中（isComposing／keyCode 229）的 Enter 不送出。
 * 送出時先清空輸入框，失敗時把原文放回，使用者不必重打。
 * @param onSend 送出處理；回傳是否成功
 */
function buildComposer(onSend: (text: string) => Promise<boolean>) {
  const box = h("textarea", {
    attrs: {
      id: "ask-question", maxlength: "2000", rows: "1", placeholder: "告訴我你想要的唇色…",
      "aria-describedby": "ask-hint",
    },
  });
  const sendBtn = h("button", { class: "cw-send", attrs: { type: "submit", "aria-label": "送出" } }, icon("send"));
  const form = h("form", { class: "cw-composer", on: { submit: (ev) => { ev.preventDefault(); void submit(box.value); } } },
    h("div", { class: "cw-composer-box" },
      h("label", { class: "sr-only", attrs: { for: "ask-question" } }, "你的需求"),
      box, sendBtn),
    h("p", { class: "cw-composer-hint", attrs: { id: "ask-hint" } },
      "Enter 送出，Shift+Enter 換行。也可以直接說「改成聚會」「取消不要霧面」來修改條件。"));

  // 依內容調整高度（最多約 6 行，超過改為捲動）；用 CSSOM 設定，不寫行內 style 屬性
  const grow = (): void => {
    box.style.height = "auto";
    box.style.height = `${Math.min(box.scrollHeight, 168)}px`;
  };
  box.addEventListener("input", grow);
  box.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter" || ev.shiftKey || ev.isComposing || ev.keyCode === 229) return;
    ev.preventDefault();
    if (!sendBtn.disabled) form.requestSubmit();
  });

  // 送出：空白不送；處理中鎖住送出鈕（輸入框仍可先打下一句）
  async function submit(raw: string): Promise<void> {
    const text = raw.trim();
    if (!text || sendBtn.disabled) return;
    sendBtn.disabled = true;
    sendBtn.setAttribute("aria-label", "處理中");
    box.value = "";
    grow();
    const ok = await onSend(text);
    if (!ok && box.value === "") { box.value = text; grow(); }
    sendBtn.disabled = false;
    sendBtn.setAttribute("aria-label", "送出");
  }

  return {
    form,
    submit,
    /** 把範例說法填進輸入框並聚焦（不自動送出）。 */
    fill(text: string): void { box.value = text; grow(); box.focus(); },
    /** 聚焦輸入框（不捲動頁面）。 */
    focus(): void { box.focus({ preventScroll: true }); },
    /** 對話讀取失敗時停用輸入。 */
    disable(): void { box.disabled = true; sendBtn.disabled = true; },
  };
}
// 核心邏輯：
// 1. 畫面上的每一則需求都是「使用者訊息＋AI 回覆」；AI 回覆一律由 result.ts 以 request_id 向後端讀回，
//    重新整理後顯示的內容與剛送出時相同，也會依此刻權限遮蔽失效的依據（S9）。
// 2. 頂部條件標籤只顯示後端摘要中「你提供」的欄位，未知或預設的條件不顯示成標籤（K1：場合未指定不當成日常）。
// 3. 不新增任何後端功能：沒有附件、照片、刪除對話或外觀切換；只重排既有功能的呈現方式。
