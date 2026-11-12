// 登入頁：帳號、密碼輸入與登入（帳號建立方式見 docs/mvp/operations.md 第 4 節）。

import { describeError } from "../api.js";
import { h, notice, replace } from "../dom.js";
import { login } from "../session.js";

export function renderLogin(view: HTMLElement, onDone: () => void): void {
  const username = h("input", { attrs: { id: "login-username", name: "username", autocomplete: "username", required: "", maxlength: "64" } });
  const password = h("input", { attrs: { id: "login-password", name: "password", type: "password", autocomplete: "current-password", required: "", maxlength: "256" } });
  const message = h("div");
  const submit = h("button", { attrs: { type: "submit" } }, "登入");
  const form = h("form", { class: "card form", on: { submit: (ev) => { ev.preventDefault(); void doLogin(); } } },
    h("label", { attrs: { for: "login-username" } }, "帳號"), username,
    h("label", { attrs: { for: "login-password" } }, "密碼"), password,
    // 登入頁不顯示帳號建立與密碼重設方式的說明（對外展示時不給看）
    submit, message);
  replace(view, h("h2", {}, "登入"), form);
  username.focus();

  async function doLogin(): Promise<void> {
    submit.disabled = true;
    replace(message);
    try {
      await login(username.value.trim(), password.value);
      password.value = "";
      onDone();
    } catch (err) {
      replace(message, notice("error", describeError(err)));
    } finally {
      submit.disabled = false;
    }
  }
}
