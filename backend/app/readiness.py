"""就緒檢查（S1）：資料庫可查詢、pgvector 已安裝、遷移已套用，且後端帳號不是超級使用者。

只執行固定、參數化的唯讀查詢；回報內容不含連線字串、主機或帳號密碼。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

from psycopg import Connection

# 就緒檢查會確認的擴充套件；以參數傳入查詢，不拼接字串
REQUIRED_EXTENSION = "vector"


@dataclass(frozen=True)
class DbStatus:
    """資料庫檢查結果。detail 只放可公開的說明，不含主機、帳號或密碼。"""

    ok: bool
    detail: str
    server_version: Optional[str] = None
    pgvector_version: Optional[str] = None
    schema_version: Optional[str] = None
    latency_ms: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)


# 在一條已取得的連線上檢查資料庫是否可供後端使用。
# 用途：給 /api/ready 呼叫；連線的取得與逾時由呼叫端（連線池）負責，這裡只做判斷。
def check_connection(conn: Connection, latency_ms: Optional[int] = None) -> DbStatus:
    server_version = _scalar(conn, "SHOW server_version")
    pgvector_version = _scalar(conn, "SELECT extversion FROM pg_extension WHERE extname = %s", (REQUIRED_EXTENSION,))
    is_superuser = bool(_scalar(conn, "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"))
    has_migrations = _scalar(conn, "SELECT to_regclass('schema_migrations') IS NOT NULL")
    schema_version = _scalar(conn, "SELECT max(version) FROM schema_migrations") if has_migrations else None

    found = dict(server_version=server_version, pgvector_version=pgvector_version,
                 schema_version=schema_version, latency_ms=latency_ms)
    if is_superuser:
        return DbStatus(ok=False, detail="後端以超級使用者連線，違反最小權限；請改用 APP_DB_USER", **found)
    if pgvector_version is None:
        return DbStatus(ok=False, detail="已連線，但 pgvector 擴充未安裝", **found)
    if schema_version is None:
        return DbStatus(ok=False, detail="已連線，但資料庫尚未套用遷移", **found)
    return DbStatus(ok=True, detail="已連線，pgvector 可用，遷移已套用", **found)
# 核心邏輯：
# 1. 依序查詢伺服器版本、pgvector 版本、目前帳號是否為超級使用者、最新遷移版本。
#    所有 SQL 都是固定語句；擴充名稱用 %s 參數傳入。
# 2. 判斷順序：超級使用者 → pgvector → 遷移。任一不符就回 ok=False，並保留已查到的版本資訊，
#    讓前端看得出卡在哪一步。
# 3. 超級使用者一律判定未就緒：資訊安全規格 v0.1 §2 要求應用 DB 帳號非超級使用者，用程式擋而不只寫在文件。
# 4. schema_migrations 表不存在（從未遷移）時不去查它，避免查詢錯誤被誤判成資料庫斷線。


# 執行查詢並回傳第一列第一欄；查無資料時回傳 None。
# 用途：讓 check_connection 的每個檢查都只寫一行，不重複處理 fetchone 為 None 的情況。
def _scalar(conn: Connection, sql: str, params: tuple = ()):
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return None
    return next(iter(row.values())) if isinstance(row, dict) else row[0]
# 核心邏輯：主程式的連線池使用 dict_row（每列是 dict），一般連線則是 tuple；
# 兩種都取第一個值，所以這個函式在測試與正式環境都能用。
