// 連線狀態頁（S1）：前端 → 後端 API → 資料庫。不需登入。

import { h, replace } from "../dom.js";

interface DatabaseStatus {
  ok: boolean;
  detail: string;
  server_version: string | null;
  pgvector_version: string | null;
  schema_version: string | null;
  latency_ms: number | null;
}

interface ReadyResponse {
  status: "ready" | "not_ready";
  api_version: string;
  time: string;
  database: DatabaseStatus;
}

function fact(label: string, value: string | number | null | undefined): HTMLElement[] {
  return [h("dt", {}, label), h("dd", {}, value === null || value === undefined ? "—" : String(value))];
}

export async function renderStatus(view: HTMLElement): Promise<void> {
  const body = h("div", {}, h("p", { class: "status", data: { state: "pending" } }, "檢查中…"));
  const again = h("button", { attrs: { type: "button" }, on: { click: () => void check() } }, "重新檢查");
  replace(view, h("h2", {}, "連線狀態"), body, again);

  async function check(): Promise<void> {
    replace(body, h("p", { class: "status", data: { state: "pending" } }, "檢查中…"));
    // 直接用 fetch：/api/ready 在資料庫未就緒時回 503，但本文仍帶狀態與原因
    let ready: ReadyResponse | null = null;
    let error = "後端未回應";
    try {
      const res = await fetch("/api/ready", { cache: "no-store", credentials: "same-origin" });
      const data = (await res.json().catch(() => null)) as ReadyResponse | null;
      if (data?.database) ready = data;
      else error = `HTTP ${res.status}`;
    } catch {
      error = "無法連線";
    }
    if (ready === null) {
      replace(body,
        h("section", { class: "card" }, h("h3", {}, "後端 API"),
          h("p", { class: "status", data: { state: "fail" } }, `未連線（${error}）`)));
      return;
    }
    const db = ready.database;
    replace(body,
      h("section", { class: "card" }, h("h3", {}, "後端 API"),
        h("p", { class: "status", data: { state: "ok" } }, "已連線"),
        h("dl", { class: "facts" }, ...fact("版本", ready.api_version), ...fact("回應時間", ready.time))),
      h("section", { class: "card" }, h("h3", {}, "資料庫（PostgreSQL + pgvector）"),
        h("p", { class: "status", data: { state: db.ok ? "ok" : "fail" } }, db.ok ? "已連線" : "未就緒"),
        h("dl", { class: "facts" },
          ...fact("說明", db.detail), ...fact("PostgreSQL", db.server_version), ...fact("pgvector", db.pgvector_version),
          ...fact("遷移版本", db.schema_version), ...fact("查詢耗時", db.latency_ms === null ? null : `${db.latency_ms} ms`))));
  }
  await check();
}
