// LipMatch AI 前端進入點：依網址 #/… 切換頁面。
// 登入狀態由後端 session cookie 決定；角色只看 /api/me 的回應，前端不自行判斷權限
// （就算改了前端，後端仍會擋下未授權的請求）。

import { describeError } from "./api.js";
import { h, notice, replace } from "./dom.js";
import { renderChat } from "./pages/chat.js";
import { renderLogin } from "./pages/login.js";
import { renderMonitor } from "./pages/monitor.js";
import { renderReview } from "./pages/review.js";
import { renderSources } from "./pages/sources.js";
import { renderStatus } from "./pages/status.js";
import { currentUser, loadSession, logout } from "./session.js";

/** 取得 index.html 中必備的頁面元素；找不到代表頁面骨架錯誤，直接丟出例外。 */
function el(id: string): HTMLElement {
  const found = document.getElementById(id);
  if (found === null) throw new Error(`找不到頁面元素 #${id}`);
  return found;
}

const nav = el("nav");
const view = el("view");

/** 共用頂欄的頁面連結（依登入狀態與角色顯示）；對話頁的 chat-mode 會把頂欄隱藏，改用側欄與帳戶選單。 */
function renderNav(active: string): void {
  const user = currentUser();
  const link = (hash: string, label: string) =>
    h("a", { class: active === hash ? "active" : "", attrs: { href: `#/${hash}` } }, label);
  if (user === null) {
    replace(nav, link("login", "登入"), link("status", "連線狀態"));
    return;
  }
  replace(nav,
    link("chat", "對話"), link("sources", "來源"),
    user.role === "admin" ? link("review", "審核") : null,
    user.role === "admin" ? link("monitor", "監控") : null,
    link("status", "連線狀態"),
    h("span", { class: "who" }, `${user.username}（${user.role === "admin" ? "管理者" : "使用者"}）`),
    h("button", { class: "link", attrs: { type: "button" }, on: { click: () => void doLogout() } }, "登出"));
}

/** 登出後回到登入頁（後端清除 session；失敗也照樣回登入頁）。 */
async function doLogout(): Promise<void> {
  await logout().catch(() => undefined);
  location.hash = "#/login";
}

/**
 * 聊天工作區（日常清新）佔滿整個視窗、隱藏共用頂欄，只在對話頁加上 body.chat-mode；
 * 登入、來源、審核、監控、連線狀態頁不加這個 class，維持原本的版面與樣式。
 */
function setChatMode(on: boolean): void {
  document.body.classList.toggle("chat-mode", on);
}

/** 依網址 #/頁面/參數 切換頁面；未登入一律顯示登入頁，管理頁只給管理者。 */
async function route(): Promise<void> {
  const [page = "", arg = null] = location.hash.replace(/^#\/?/, "").split("/");
  const user = currentUser();
  setChatMode(false);
  try {
    if (page === "status") {
      renderNav("status");
      await renderStatus(view);
      return;
    }
    if (user === null) {
      renderNav("login");
      renderLogin(view, () => { location.hash = "#/chat"; void route(); });
      return;
    }
    if (page === "review" && user.role === "admin") {
      renderNav("review");
      await renderReview(view);
    } else if (page === "monitor" && user.role === "admin") {
      // S10：管理者監控頁（後端 /api/admin/monitor/* 另外檢查管理者權限）
      renderNav("monitor");
      await renderMonitor(view);
    } else if (page === "sources") {
      renderNav("sources");
      await renderSources(view);
    } else {
      renderNav("chat");
      setChatMode(true);
      await renderChat(view, page === "chat" && arg ? decodeURIComponent(arg) : null);
    }
  } catch (err) {
    setChatMode(false);
    replace(view, notice("error", describeError(err)));
  }
}

window.addEventListener("hashchange", () => void route());

void (async () => {
  try {
    await loadSession();
  } catch (err) {
    replace(view, notice("error", describeError(err)));
  }
  await route();
})();
