// 後端 API 呼叫：同源 /api/（由 nginx 轉給後端），session 只在 HttpOnly cookie，前端不保存任何金鑰。
// CSRF 權杖只放在記憶體，會修改資料的請求帶在 X-CSRF-Token 標頭。

const API_BASE = "/api";
const REQUEST_TIMEOUT_MS = 15000;
// 送出問題要等模型生成：實測每題 14～53 秒（2026-11-06 40 題重跑，中位 38 秒）。比 nginx 的 120 秒稍長，
// 讓 nginx 先回 504；兩種情況前端都說「還在整理」，不說「沒有送出」（後端收到後會照常做完並存檔）。
export const MESSAGE_TIMEOUT_MS = 130000;
const UNSAFE = new Set(["POST", "PUT", "PATCH", "DELETE"]);

let csrfToken: string | null = null;

/** 登入或 /api/me 取得 CSRF 權杖後存在記憶體；登出時清除。 */
export function setCsrfToken(token: string | null): void {
  csrfToken = token;
}

/** 後端統一錯誤格式 {"error": {code, message, request_id}} 轉成例外。 */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly requestId: string | null,
  ) {
    super(message);
  }
}

interface ErrorBody {
  error?: { code?: string; message?: string; request_id?: string };
}

/**
 * 呼叫後端並解析 JSON；非 2xx 一律丟出 ApiError（帶 request_id 方便回報）。
 * @param method HTTP 方法
 * @param path 例如 "/conversations"
 * @param body 會以 JSON 送出；GET 不帶
 * @param timeoutMs 逾時毫秒數；預設 15 秒，送出問題用 MESSAGE_TIMEOUT_MS
 * @param extraHeaders 額外標頭（progress-hint-v1：送出問題時帶 X-Progress-Token）；不能覆寫 CSRF 與內容型別
 */
export async function api<T>(method: string, path: string, body?: unknown,
  timeoutMs: number = REQUEST_TIMEOUT_MS, extraHeaders: Record<string, string> = {}): Promise<T> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  const headers: Record<string, string> = { ...extraHeaders, Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (UNSAFE.has(method) && csrfToken !== null) headers["X-CSRF-Token"] = csrfToken;
  try {
    const res = await fetch(API_BASE + path, {
      method,
      headers,
      credentials: "same-origin",
      cache: "no-store",
      signal: controller.signal,
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    if (res.status === 204) return undefined as T;
    const data = (await res.json().catch(() => null)) as unknown;
    if (!res.ok) {
      const err = (data as ErrorBody | null)?.error;
      throw new ApiError(res.status, err?.code ?? "http_error", err?.message ?? `HTTP ${res.status}`,
        err?.request_id ?? res.headers.get("X-Request-ID"));
    }
    return data as T;
  } catch (err) {
    if (err instanceof ApiError) throw err;
    const timeout = err instanceof DOMException && err.name === "AbortError";
    throw new ApiError(0, timeout ? "timeout" : "network_error", timeout ? "伺服器回應逾時" : "無法連線到伺服器", null);
  } finally {
    window.clearTimeout(timer);
  }
}
// 核心邏輯：回應內容以型別標註但不信任；畫面一律用 textContent 顯示，格式不符也不會被執行。

/** 把錯誤轉成給使用者看的一句話（附 request_id）。 */
export function describeError(err: unknown): string {
  if (err instanceof ApiError) {
    return err.requestId ? `${err.message}（request_id：${err.requestId}）` : err.message;
  }
  return "發生未預期的錯誤";
}
