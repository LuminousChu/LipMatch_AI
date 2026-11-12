// 聊天工作區（日常清新）使用的細線圖示與品牌標誌。
// 一律以 document.createElementNS 建立 SVG 節點，不使用 innerHTML，也不寫行內 style
// （nginx 的 CSP 為 style-src 'self'，顏色與尺寸都交給 styles.css 的 class 控制）。
// 圖示預設 aria-hidden；按鈕的可讀名稱由呼叫端以 aria-label 或文字提供。

const SVG_NS = "http://www.w3.org/2000/svg";

/** 一個圖示由數個圖形組成：path 的 d 字串，或圓形 [cx, cy, r]。 */
type Shape = string | [number, number, number];

/** 圖示名稱 → 圖形（24×24 座標、細線描邊）。新增圖示只要在這裡加一筆。 */
const ICONS = {
  plus: ["M12 5v14", "M5 12h14"],
  chat: ["M4 6.5A2.5 2.5 0 0 1 6.5 4h11A2.5 2.5 0 0 1 20 6.5v7a2.5 2.5 0 0 1-2.5 2.5H10l-4.4 3.5c-.4.3-.9 0-.9-.4V16.3A2.5 2.5 0 0 1 4 14z"],
  edit: ["M4 20h4L19 9a2.1 2.1 0 0 0-3-3L5 17z", "M14 7l3 3"],
  sliders: ["M4 7h9", "M17 7h3", "M4 17h3", "M11 17h9", [15, 7, 2], [9, 17, 2]],
  user: [[12, 8.5, 3.5], "M5 20c1.3-3.4 4-5 7-5s5.7 1.6 7 5"],
  thumbUp: ["M7 11v9H4.5a.5.5 0 0 1-.5-.5v-8a.5.5 0 0 1 .5-.5z",
    "M7 11l3.6-6.6c.3-.5.8-.7 1.3-.5 1.2.4 1.8 1.6 1.5 2.8L12.8 10h5.4a2 2 0 0 1 2 2.4l-1.3 6a2 2 0 0 1-2 1.6H7"],
  thumbDown: ["M17 13V4h2.5a.5.5 0 0 1 .5.5v8a.5.5 0 0 1-.5.5z",
    "M17 13l-3.6 6.6c-.3.5-.8.7-1.3.5-1.2-.4-1.8-1.6-1.5-2.8l.6-3.3H5.8a2 2 0 0 1-2-2.4l1.3-6a2 2 0 0 1 2-1.6H17"],
  copy: ["M9 9h9.5a1.5 1.5 0 0 1 1.5 1.5V19a1.5 1.5 0 0 1-1.5 1.5H10A1.5 1.5 0 0 1 8.5 19V9.5", "M15.5 9V5.5A1.5 1.5 0 0 0 14 4H5.5A1.5 1.5 0 0 0 4 5.5V14a1.5 1.5 0 0 0 1.5 1.5H8.5"],
  check: ["M5 12.5l4.5 4.5L19 7.5"],
  send: ["M4.5 11.6 19 4.8c.5-.2 1 .3.8.8L13 20c-.2.5-.9.5-1.1 0l-2.2-6-6-2.2c-.5-.2-.5-.9 0-1.1z", "M9.8 14 19.5 5"],
  arrowRight: ["M5 12h14", "M13 6l6 6-6 6"],
  menu: ["M4 7h16", "M4 12h16", "M4 17h16"],
  close: ["M6 6l12 12", "M18 6 6 18"],
  sources: ["M5 5.5A1.5 1.5 0 0 1 6.5 4H17a1 1 0 0 1 1 1v14H7a2 2 0 0 1-2-2z", "M5 17a2 2 0 0 1 2-2h11"],
  review: ["M12 3.5 19 6v5.2c0 4.2-2.9 7.6-7 9.3-4.1-1.7-7-5.1-7-9.3V6z", "M9 12l2.2 2.2L15.5 10"],
  monitor: ["M5 20v-8", "M12 20V5", "M19 20v-5", "M3 20h18"],
  status: ["M5 18v-2", "M9.5 18v-5", "M14 18V9", "M18.5 18V5.5"],
  logout: ["M14 4h4a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-4", "M9.5 8 5.5 12l4 4", "M5.5 12H15"],
  sparkle: ["M12 4l1.6 4.6L18 10.2l-4.4 1.6L12 16.4l-1.6-4.6L6 10.2l4.4-1.6z"],
  lips: ["M3 12c2.4-3.7 4.8-5.3 6.4-5.3 1.1 0 1.9.6 2.6 1.3.7-.7 1.5-1.3 2.6-1.3 1.6 0 4 1.6 6.4 5.3-2.4 3.4-5.3 5.3-9 5.3S5.4 15.4 3 12z", "M3 12c3 .9 6 1.3 9 1.3s6-.4 9-1.3"],
  info: [[12, 12, 8.5], "M12 11v5", "M12 8h.01"],
  trash: ["M4.5 7h15", "M9.5 7V5.5A1.5 1.5 0 0 1 11 4h2a1.5 1.5 0 0 1 1.5 1.5V7",
    "M6.5 7l.8 11.2A2 2 0 0 0 9.3 20h5.4a2 2 0 0 0 2-1.8L17.5 7", "M10.5 11v5", "M13.5 11v5"],
} satisfies Record<string, Shape[]>;

export type IconName = keyof typeof ICONS;

/** 建立單一 SVG 子元素並設定屬性。 */
function svgEl(tag: string, attrs: Record<string, string>): SVGElement {
  const el = document.createElementNS(SVG_NS, tag) as SVGElement;
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

/**
 * 細線圖示：描邊顏色跟隨文字色（currentColor），粗細與大小由 CSS 的 .icon 控制。
 * @param name 圖示名稱（見 ICONS）
 * @param extraClass 額外的 class（例如調整尺寸）
 */
export function icon(name: IconName, extraClass = ""): SVGElement {
  const svg = svgEl("svg", {
    class: `icon icon-${name}${extraClass ? ` ${extraClass}` : ""}`,
    viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
    "stroke-width": "1.6", "stroke-linecap": "round", "stroke-linejoin": "round",
    "aria-hidden": "true", focusable: "false",
  });
  for (const shape of ICONS[name] as Shape[]) {
    svg.appendChild(typeof shape === "string"
      ? svgEl("path", { d: shape })
      : svgEl("circle", { cx: String(shape[0]), cy: String(shape[1]), r: String(shape[2]) }));
  }
  return svg;
}

/**
 * 品牌葉片標誌：一片苔綠、一片玫瑰粉（顏色在 CSS 的 .brand-leaf-a／-b 指定）。
 * 純裝飾，aria-hidden；旁邊的「LipMatch AI」文字才是可讀名稱。
 */
export function leafLogo(extraClass = ""): SVGElement {
  const svg = svgEl("svg", {
    class: `brand-leaf${extraClass ? ` ${extraClass}` : ""}`,
    viewBox: "0 0 24 24", "aria-hidden": "true", focusable: "false",
  });
  svg.appendChild(svgEl("path", { class: "brand-leaf-b", d: "M10.6 20.5C6.2 18.8 3.7 14.6 4.6 9.3c3.9 1.5 6.6 5.6 6 11.2z" }));
  svg.appendChild(svgEl("path", { class: "brand-leaf-a", d: "M12.2 21c-1.4-6.4.6-12.9 7-17.5 1.6 6.6-1 14-7 17.5z" }));
  return svg;
}
// 核心邏輯：圖示只是外觀；所有可操作元素的名稱都由呼叫端以文字或 aria-label 提供，
// 讀屏軟體不會讀到 SVG 本身（aria-hidden="true"、focusable="false"）。
