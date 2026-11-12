// 對話頁的「修改條件」視窗：需求摘要（後端產生）＋需求設定表單（S6）。
// 由聊天頂部的「修改條件」或條件標籤開啟；內容與舊版對話頁的摘要卡片、設定表單相同，只是改放進對話框，
// 讓聊天主畫面保持簡潔。偏好規則一律在後端（PATCH /api/conversations/{id}），前端不自行推定任何條件。

import { api, describeError } from "../api.js";
import { h, notice, replace, splitTerms } from "../dom.js";
import { icon } from "../icons.js";
import {
  type Allergy, type ConversationDetail, type Finish, type PrefItem, type Scenario, type Summary, type Undertone,
  SCENARIOS, STATUS_LABEL,
} from "./chat-types.js";

const FINISHES: [Finish | "", string][] = [["", "不指定"], ["matte", "霧面"], ["satin", "緞光"], ["gloss", "亮面"], ["any", "都可以"]];
const UNDERTONES: [Undertone, string][] = [["unknown", "未知／不確定"], ["warm", "暖底調"], ["cool", "冷底調"], ["neutral", "中性底調"]];
const ALLERGY: [Allergy, string][] = [
  ["unanswered", "尚未回答"], ["none_known", "沒有已知過敏"], ["has_known", "有已知過敏"], ["unsure", "不確定"],
];

/**
 * 單選按鈕群組（外觀為圓角選項標籤）。
 * value 為空字串時不預選任何選項（例如場合未指定，K1 不把未指定當成日常）。
 */
function radioGroup<T extends string>(name: string, legend: string, options: [T, string][], value: T | "",
  onChange?: () => void): HTMLElement {
  return h("fieldset", { class: "cw-field cw-choices" },
    h("legend", {}, legend),
    h("div", { class: "cw-choice-row" }, ...options.map(([v, label]) => {
      const input = h("input", { attrs: { type: "radio", name, value: v }, on: onChange ? { change: onChange } : {} });
      input.checked = v === value;
      return h("label", { class: "cw-choice" }, input, h("span", {}, label));
    })));
}

/** 取得單選群組目前選取的值；沒有選取時回傳空字串。 */
function checkedValue(form: HTMLElement, name: string): string {
  return form.querySelector<HTMLInputElement>(`input[name="${name}"]:checked`)?.value ?? "";
}

/** 清單欄位 → 表單輸入框的文字（用顯示名稱，後端可以對回原本的鍵）。 */
function labels(items: PrefItem[] | undefined): string {
  return (items ?? []).map((i) => i.label).join("、");
}

/** 一個「標籤＋輸入元件＋選填說明」的欄位區塊。 */
function field(id: string, label: string, control: HTMLElement, hint?: string): HTMLElement {
  return h("div", { class: "cw-field" },
    h("label", { attrs: { for: id } }, label),
    control,
    hint ? h("p", { class: "cw-hint" }, hint) : null);
}

/**
 * 待確認的衝突與模糊詞（後端產生），轉成給使用者看的句子。
 * 這些項目不會被系統自行決定，聊天頂部也會提示數量。
 */
export function pendingItems(summary: Summary): string[] {
  return [
    ...summary.conflicts.map((c) => `衝突：${c.labels ? c.labels.join("／") : c.label ?? c.field}（你說的：「${c.raw}」）`),
    ...summary.ambiguities.map((a) => `待確認的說法：「${a.raw}」`),
  ];
}

/**
 * 需求摘要：逐欄列出目前條件、來源（你提供／預設／未知）、原話，以及待確認的衝突與模糊詞。
 * 內容全部由後端產生，前端不重新解讀。
 */
function renderSummary(summary: Summary): HTMLElement {
  const rows = summary.rows.map((r) => {
    const raw = Array.isArray(r.raw) ? r.raw.join("；") : r.raw;
    return h("tr", {},
      h("th", { attrs: { scope: "row" } }, r.label),
      h("td", {}, r.value, r.hard ? h("span", { class: "badge warn" }, "排除條件") : null,
        r.note ? h("div", { class: "note" }, r.note) : null),
      h("td", {}, h("span", { class: "badge" }, STATUS_LABEL[r.status] ?? r.status)),
      h("td", { class: "note" }, raw ? `「${raw}」` : "—"));
  });
  const pending = pendingItems(summary);
  return h("section", { class: "cw-panel" },
    h("h3", {}, "需求摘要"),
    h("p", { class: "cw-hint" }, "這是系統目前記下的條件，只保存在這個對話。有誤可以直接在對話中說「改成…」「取消…」，或用下方表單修改。"),
    h("div", { class: "table-wrap" },
      h("table", { class: "table cw-table" },
        h("thead", {}, h("tr", {}, h("th", {}, "項目"), h("th", {}, "目前條件"), h("th", {}, "來源"), h("th", {}, "你的原話"))),
        h("tbody", {}, ...rows))),
    pending.length > 0 ? h("h4", {}, "尚未確認（不會被自行決定）") : null,
    pending.length > 0 ? h("ul", { class: "cw-pending" }, ...pending.map((p) => h("li", {}, p))) : null);
}

/**
 * 需求設定表單。儲存成功後呼叫 onSaved（由呼叫端重新讀取對話、更新頂部條件與側欄）。
 * @param conv 目前的對話（含偏好狀態）
 * @param onSaved 儲存成功後的回呼
 */
function renderForm(conv: ConversationDetail, onSaved: () => void): HTMLFormElement {
  const prefs = conv.preferences;
  const title = h("input", { attrs: { id: "conv-title", maxlength: "200", value: conv.title ?? "" } });
  const liked = h("input", { attrs: { id: "pref-liked", value: labels(prefs.liked_colors), placeholder: "例如：玫瑰色、裸色" } });
  const disliked = h("input", {
    attrs: { id: "pref-disliked", value: [labels(prefs.disliked_colors), labels(prefs.disliked_finishes)].filter(Boolean).join("、"),
      placeholder: "例如：橘色、霧面" },
  });
  const firstFinish = (prefs.liked_finishes[0]?.value ?? "") as Finish | "";
  const finish = h("select", { attrs: { id: "pref-finish" } }, ...FINISHES.map(([v, label]) => {
    const o = h("option", { attrs: { value: v } }, label);
    o.selected = firstFinish === v;
    return o;
  }));
  const effects = h("input", { attrs: { id: "pref-effects", value: labels(prefs.effects), placeholder: "例如：自然、有氣色" } });
  const skin = h("input", { attrs: { id: "pref-skin", maxlength: "50", value: prefs.skin_tone.value ?? "", placeholder: "用自己的話描述，例如：小麥色" } });
  const undertone = h("select", { attrs: { id: "pref-undertone" } }, ...UNDERTONES.map(([v, label]) => {
    const o = h("option", { attrs: { value: v } }, label);
    o.selected = (prefs.undertone.value ?? "unknown") === v;
    return o;
  }));
  const lip = h("input", { attrs: { id: "pref-lip", maxlength: "50", value: prefs.natural_lip.value ?? "", placeholder: "例如：偏深、偏紫（不知道可留空）" } });
  const notes = h("textarea", { attrs: { id: "pref-notes", maxlength: "500", rows: "2" } });
  notes.value = prefs.notes.value ?? "";
  const avoid = h("input", { attrs: { id: "allergy-avoid", value: conv.allergy_avoid.join("、"), placeholder: "例如：某成分名稱" } });
  const saveMsg = h("div", { attrs: { "aria-live": "polite" } });
  const saveBtn = h("button", { class: "cw-btn-primary", attrs: { type: "submit" } }, "儲存設定");

  const form = h("form", { class: "cw-panel cw-form", on: { submit: (ev) => { ev.preventDefault(); void save(); } } },
    h("h3", {}, "需求設定"),
    field("conv-title", "對話名稱", title),
    radioGroup("scenario", "情境（未選代表未指定，不會自動當成日常）", SCENARIOS,
      conv.scenario_source === "user" ? conv.scenario : ""),
    h("div", { class: "cw-grid-2" },
      field("pref-effects", "希望效果", effects),
      field("pref-liked", "喜歡的色系", liked, "用頓號或逗號分隔"),
      field("pref-disliked", "不要的色系或妝感", disliked, "排除條件；改情境時會保留"),
      field("pref-finish", "喜歡的質地", finish),
      field("pref-skin", "自述膚色", skin),
      field("pref-undertone", "底調", undertone, "只有你確定才選；系統不從膚色推定"),
      field("pref-lip", "已知的原生唇色", lip)),
    field("pref-notes", "其他說明", notes),
    radioGroup("allergy", "過敏狀態", ALLERGY, conv.allergy_status, () => syncAvoid()),
    field("allergy-avoid", "需要避開的成分或產品", avoid, "只在「有已知過敏」或「不確定」時填寫"),
    h("p", { class: "cw-hint" }, "這裡只記錄你要避開的項目，系統不做過敏診斷，也不保證任何產品安全；有疑慮請諮詢專業人員。"),
    h("div", { class: "cw-form-actions" }, saveBtn), saveMsg);
  syncAvoid();
  return form;

  // 過敏狀態為未回答／無已知時，需避開項目不能填（與後端規則一致，規範 6 的四種狀態不混淆）
  function syncAvoid(): void {
    const status = checkedValue(form, "allergy") as Allergy;
    avoid.disabled = !(status === "has_known" || status === "unsure");
    if (avoid.disabled) avoid.value = "";
  }

  // 儲存表單：後端以同一組偏好規則套用，只改有帶的欄位
  async function save(): Promise<void> {
    replace(saveMsg);
    const scenario = checkedValue(form, "scenario") as Scenario | "";
    // 未指定時沒有預選任何情境；只有使用者選了才送出（K1：不把未指定存成你提供的日常）
    const scenarioPart = scenario ? { scenario } : {};
    const body = {
      title: title.value.trim() || null,
      ...scenarioPart,
      preferences: {
        liked: splitTerms(liked.value), disliked: splitTerms(disliked.value),
        finish: (finish.value || null) as Finish | null, effects: splitTerms(effects.value),
        skin_tone: skin.value.trim() || null, undertone: undertone.value as Undertone,
        natural_lip: lip.value.trim() || null, notes: notes.value.trim() || null,
      },
      allergy_status: checkedValue(form, "allergy") as Allergy,
      allergy_avoid: avoid.disabled ? [] : splitTerms(avoid.value),
    };
    saveBtn.disabled = true;
    try {
      await api("PATCH", `/conversations/${encodeURIComponent(conv.id)}`, body);
      onSaved();
    } catch (err) {
      replace(saveMsg, notice("error", describeError(err)));
      saveBtn.disabled = false;
    }
  }
}

/**
 * 開啟「修改條件」對話框（原生 <dialog>，Esc 或關閉鈕可關閉，焦點由瀏覽器管理）。
 * 儲存成功後重新讀取對話並把新的摘要畫回同一個視窗，並呼叫 onChanged 讓聊天頂部與側欄同步。
 * @param host 對話框要掛載的容器（聊天工作區根節點，樣式才會套用）
 * @param conv 目前的對話
 * @param onChanged 條件或名稱變更後的回呼，參數是重新讀取到的對話
 */
export function openSettingsDialog(host: HTMLElement, conv: ConversationDetail,
  onChanged: (fresh: ConversationDetail) => void): void {
  const body = h("div", { class: "cw-dialog-body" });
  const closeBtn = h("button", { class: "cw-icon-btn", attrs: { type: "button", "aria-label": "關閉" } }, icon("close"));
  const dialog = h("dialog", { class: "cw-dialog", attrs: { "aria-labelledby": "cw-dialog-title" } },
    h("header", { class: "cw-dialog-head" },
      h("h2", { attrs: { id: "cw-dialog-title" } }, "修改條件"), closeBtn),
    body);
  closeBtn.addEventListener("click", () => dialog.close());
  // 點擊對話框外的半透明背景也關閉（點到的是 dialog 本身而不是內容時）
  dialog.addEventListener("click", (ev) => { if (ev.target === dialog) dialog.close(); });
  dialog.addEventListener("close", () => dialog.remove());

  // 依目前對話畫出摘要與表單；saved 為 true 時在最上方顯示「設定已儲存」
  function fill(current: ConversationDetail, saved: boolean): void {
    replace(body,
      saved ? notice("ok", "設定已儲存。") : null,
      renderSummary(current.summary),
      renderForm(current, () => void refresh()));
  }

  // 儲存後重新讀取對話（摘要由後端重新產生），失敗時保留視窗並顯示錯誤
  async function refresh(): Promise<void> {
    try {
      const fresh = await api<ConversationDetail>("GET", `/conversations/${encodeURIComponent(conv.id)}`);
      fill(fresh, true);
      body.scrollTop = 0;
      onChanged(fresh);
    } catch (err) {
      body.prepend(notice("error", describeError(err)));
    }
  }

  fill(conv, false);
  host.appendChild(dialog);
  dialog.showModal();
}
// 核心邏輯：
// 1. 視窗內容＝後端摘要＋表單，儲存一律走 PATCH，由後端套用偏好規則（只改有帶的欄位）。
// 2. 儲存後重新讀取對話再畫摘要，畫面顯示的永遠是後端的版本，不是前端自己的推測。
