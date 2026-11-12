// 目前登入者：角色只來自後端 /api/me（由 session cookie 決定），前端不自行判斷或保存角色。

import { api, ApiError, setCsrfToken } from "./api.js";

export interface User {
  id: number;
  username: string;
  role: "user" | "admin";
}

interface MeResponse extends User {
  csrf_token: string;
}

interface LoginResponse {
  csrf_token: string;
  user: User;
}

let current: User | null = null;

export function currentUser(): User | null {
  return current;
}

/** 重新整理頁面後，用 cookie 向後端取回登入者與 CSRF 權杖；未登入回傳 null。 */
export async function loadSession(): Promise<User | null> {
  try {
    const me = await api<MeResponse>("GET", "/me");
    setCsrfToken(me.csrf_token);
    current = { id: me.id, username: me.username, role: me.role };
  } catch (err) {
    if (!(err instanceof ApiError) || err.status !== 401) throw err;
    current = null;
    setCsrfToken(null);
  }
  return current;
}

export async function login(username: string, password: string): Promise<User> {
  const res = await api<LoginResponse>("POST", "/auth/login", { username, password });
  setCsrfToken(res.csrf_token);
  current = res.user;
  return res.user;
}

export async function logout(): Promise<void> {
  try {
    await api<void>("POST", "/auth/logout");
  } finally {
    current = null;
    setCsrfToken(null);
  }
}
