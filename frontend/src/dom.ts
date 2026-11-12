// 建立 DOM 的小工具：文字一律走 textContent／text node，不使用 innerHTML，
// 來源原文、使用者輸入、API 錯誤訊息都不會被當成 HTML 執行。

export type Child = Node | string | number | null | undefined | false;

export interface Props {
  class?: string;
  attrs?: Record<string, string>;
  data?: Record<string, string>;
  on?: Partial<Record<keyof HTMLElementEventMap, (ev: Event) => void>>;
}

/** 建立元素並加入子節點（字串轉成文字節點）。 */
export function h<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  props: Props = {},
  ...children: Child[]
): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  if (props.class) el.className = props.class;
  for (const [k, v] of Object.entries(props.attrs ?? {})) {
    if (k.toLowerCase().startsWith("on")) throw new Error(`不允許以屬性設定事件：${k}`);
    el.setAttribute(k, v);
  }
  for (const [k, v] of Object.entries(props.data ?? {})) el.dataset[k] = v;
  for (const [ev, fn] of Object.entries(props.on ?? {})) {
    if (fn) el.addEventListener(ev, fn);
  }
  append(el, ...children);
  return el;
}

export function append(parent: Node, ...children: Child[]): void {
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    parent.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
  }
}

/** 清空容器後放入新內容。 */
export function replace(parent: HTMLElement, ...children: Child[]): void {
  parent.replaceChildren();
  append(parent, ...children);
}

/** 顯示訊息列（成功／錯誤／提示／警告）。 */
export function notice(kind: "ok" | "error" | "info" | "warn", text: string): HTMLParagraphElement {
  return h("p", { class: `notice notice-${kind}`, attrs: { role: kind === "error" ? "alert" : "status" } }, text);
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString("zh-TW", { hour12: false });
}

/** 逗號、頓號或換行分隔的詞 → 陣列（去空白、去重複）。 */
export function splitTerms(text: string): string[] {
  return [...new Set(text.split(/[,，、\n]/).map((t) => t.trim()).filter((t) => t.length > 0))];
}
