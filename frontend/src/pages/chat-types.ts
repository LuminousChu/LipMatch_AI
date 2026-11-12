// 對話頁共用的型別與顯示名稱（chat.ts 與 chat-settings.ts 共用）。
// 型別只描述後端回應的形狀，前端不信任也不重新解讀：偏好的解析、合併、衝突判斷都在後端（preferences.py）。

export type Scenario = "daily" | "office" | "party";
export type Allergy = "unanswered" | "none_known" | "has_known" | "unsure";
export type Finish = "matte" | "satin" | "gloss" | "any";
export type Undertone = "warm" | "cool" | "neutral" | "unknown";

/** 清單欄位的一個項目：正規化鍵、顯示名稱、使用者原話。 */
export interface PrefItem {
  value: string;
  label: string;
  raw: string | null;
}

/** 單值欄位：value 為 null 代表未知。 */
export interface PrefScalar {
  value: string | null;
  status: string;
  raw: string | null;
}

/** 後端 pref-v1 偏好狀態（只列前端用得到的欄位）。 */
export interface PreferenceState {
  skin_tone: PrefScalar;
  undertone: PrefScalar;
  natural_lip: PrefScalar;
  notes: PrefScalar;
  effects: PrefItem[];
  liked_colors: PrefItem[];
  disliked_colors: PrefItem[];
  liked_finishes: PrefItem[];
  disliked_finishes: PrefItem[];
}

/** 需求摘要的一列（後端 preferences.summarize 產生）。 */
export interface SummaryRow {
  field: string;
  label: string;
  value: string;
  status: string;
  raw: string | string[] | null;
  hard: boolean;
  note: string | null;
}

/** 待確認的衝突或模糊詞。 */
export interface PendingItem {
  field: string;
  label?: string;
  labels?: string[];
  raw: string;
}

export interface Summary {
  rows: SummaryRow[];
  conflicts: PendingItem[];
  ambiguities: PendingItem[];
}

export interface Conversation {
  id: string;
  title: string | null;
  scenario: Scenario;
  // K1（2026-10-18）：default 代表「未指定單一場合」（資料表欄位不可為空才存佔位值），畫面不可顯示成日常
  scenario_source: "default" | "user";
  preferences: PreferenceState;
  summary: Summary;
  allergy_status: Allergy;
  allergy_avoid: string[];
  created_at: string;
  updated_at: string;
}

/** 對話中的一次需求（GET /api/conversations/{id} 的 requests）。 */
export interface RequestRow {
  request_id: string;
  question: string;
  status: "started" | "succeeded" | "failed";
  answer: string | null;
  /** S9：依此刻權限，這則舊回答的依據已不可用，後端已把回答文字遮蔽。 */
  answer_masked: boolean;
  outcome: string | null;
  intent: string | null;
  started_at: string;
  feedback_rating: 1 | -1 | null;
}

export interface ConversationDetail extends Conversation {
  requests: RequestRow[];
}

export const SCENARIOS: [Scenario, string][] = [["daily", "日常自然"], ["office", "職場低調"], ["party", "聚會／鮮明"]];
export const SCENARIO_LABEL = Object.fromEntries(SCENARIOS) as Record<Scenario, string>;

/** 摘要列的來源標示（你提供／預設／未知…）。 */
export const STATUS_LABEL: Record<string, string> = {
  user: "你提供", default: "預設", unknown: "未知", unanswered: "未回答", tentative: "暫定（你不確定）",
  none_known: "你提供", has_known: "你提供", unsure: "你提供",
};
