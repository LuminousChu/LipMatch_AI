"""登入失敗限制（資訊安全規格 v0.1 §4：登入另限制連續失敗，回應 429）。

兩種限制同時檢查，任一達到上限就拒絕，且在驗證密碼之前就拒絕：
- 同一帳號名稱：上次成功登入之後、時間窗內的連續失敗次數。
- 同一來源 IP：時間窗內的失敗次數（避免換帳號名稱猜密碼）。

紀錄存在 PostgreSQL（auth_login_attempts），不新增 Redis 等服務。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from psycopg import Connection

# 登入紀錄最多保留的時間；超過就刪除，避免無限累積
RETENTION_HOURS = 24


@dataclass(frozen=True)
class LoginLimits:
    """登入限制參數；數值為規格建議的起點，可由環境變數調整。"""

    max_failures_per_user: int = 5
    max_failures_per_ip: int = 20
    window_minutes: int = 15


# 檢查這次登入是否還允許嘗試。
# 用途：在驗證密碼前呼叫；回傳 None 代表允許，回傳整數代表需等待的秒數（給 Retry-After）。
def seconds_until_allowed(conn: Connection, username: str, client_ip: str, limits: LoginLimits) -> Optional[int]:
    user_count, user_wait = _row(conn.execute(
        """SELECT count(*),
                  ceil(extract(epoch FROM min(attempted_at) + make_interval(mins => %(w)s) - now()))::int
             FROM auth_login_attempts
            WHERE username = %(u)s AND NOT succeeded
              AND attempted_at > now() - make_interval(mins => %(w)s)
              AND attempted_at > COALESCE(
                    (SELECT max(attempted_at) FROM auth_login_attempts WHERE username = %(u)s AND succeeded),
                    '-infinity')""",
        {"u": username, "w": limits.window_minutes},
    ).fetchone())
    ip_count, ip_wait = _row(conn.execute(
        """SELECT count(*),
                  ceil(extract(epoch FROM min(attempted_at) + make_interval(mins => %(w)s) - now()))::int
             FROM auth_login_attempts
            WHERE client_ip = %(ip)s AND NOT succeeded
              AND attempted_at > now() - make_interval(mins => %(w)s)""",
        {"ip": client_ip, "w": limits.window_minutes},
    ).fetchone())

    waits = []
    if user_count >= limits.max_failures_per_user:
        waits.append(user_wait)
    if ip_count >= limits.max_failures_per_ip:
        waits.append(ip_wait)
    if not waits:
        return None
    return max(1, max(w or 1 for w in waits))
# 核心邏輯：
# 1. 帳號限制只算「上次成功登入之後」的失敗，所以成功登入一次就重新計算（連續失敗）。
# 2. 兩種限制都只看時間窗內的失敗；最早那筆失敗離開時間窗，就會空出一次嘗試機會，
#    所以等待秒數 = 最早一筆失敗時間 + 時間窗 − 現在。
# 3. 兩個條件都達到時取較長的等待時間；至少回 1 秒，避免回傳 0 讓用戶端立刻重試。
# 4. 全部是固定 SQL＋參數；時間一律用資料庫的 now()，不受應用程式所在主機時鐘影響。


# 記錄一次登入嘗試，並順便清除過期紀錄。
# 用途：每次真的驗證過密碼後呼叫（被 429 擋下的請求不記錄，避免攻擊者不斷延長封鎖時間）。
def record_attempt(conn: Connection, username: str, client_ip: str, succeeded: bool) -> None:
    conn.execute(
        "INSERT INTO auth_login_attempts (username, client_ip, succeeded) VALUES (%s, %s, %s)",
        (username, client_ip, succeeded),
    )
    conn.execute(
        "DELETE FROM auth_login_attempts WHERE attempted_at < now() - make_interval(hours => %s)",
        (RETENTION_HOURS,),
    )
# 核心邏輯：成功與失敗都記錄（成功紀錄是「連續失敗」重新計算的依據）；
# 清除超過保留時間的紀錄，讓這張表只保留限流需要的資料。


# 取出查詢結果的兩個欄位；相容 dict_row 與 tuple 兩種列格式。
def _row(row) -> tuple[int, Optional[int]]:
    if isinstance(row, dict):
        values = list(row.values())
        return int(values[0]), values[1]
    return int(row[0]), row[1]
# 核心邏輯：主程式連線池使用 dict_row，測試直接連線則是 tuple；兩種都依欄位順序取值。
