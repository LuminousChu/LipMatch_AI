"""後端設定：全部從環境變數讀取，沒有內建密碼或預設帳號。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from .login_limit import LoginLimits


@dataclass(frozen=True)
class Settings:
    database_url: str
    session_ttl_hours: int = 12
    db_pool_max: int = 10
    # cookie 是否加 Secure：預設開啟；只有本機 http 開發才在環境變數關閉
    session_cookie_secure: bool = True
    # 允許發出「會修改資料的請求」的來源（Origin），例如 http://127.0.0.1:8280；空白代表一律拒絕
    allowed_origins: tuple[str, ...] = ()
    # 是否採信反向代理（nginx）設定的 X-Real-IP 當作用戶端 IP；只有後端前面確實有代理時才開
    trust_proxy_headers: bool = False
    login_limits: LoginLimits = field(default_factory=LoginLimits)


# 把環境變數轉成布林值；只接受明確的 true／false 寫法。
# 用途：避免拼錯（例如 "flase"）被當成 True 或 False 而悄悄改變安全設定。
def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if raw == "":
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} 只能是 true 或 false，目前是 {raw!r}")
# 核心邏輯：沒設定時用預設值；設定了但不是可辨識的值就讓後端啟動失敗，而不是猜測。


def load_settings() -> Settings:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("請設定 DATABASE_URL")
    origins = tuple(o.strip().rstrip("/") for o in os.environ.get("APP_ORIGINS", "").split(",") if o.strip())
    return Settings(
        database_url=url,
        session_ttl_hours=int(os.environ.get("SESSION_TTL_HOURS", "12")),
        db_pool_max=int(os.environ.get("DB_POOL_MAX", "10")),
        session_cookie_secure=_env_bool("SESSION_COOKIE_SECURE", True),
        allowed_origins=origins,
        trust_proxy_headers=_env_bool("TRUST_PROXY_HEADERS", False),
        login_limits=LoginLimits(
            max_failures_per_user=int(os.environ.get("LOGIN_MAX_FAILURES_PER_USER", "5")),
            max_failures_per_ip=int(os.environ.get("LOGIN_MAX_FAILURES_PER_IP", "20")),
            window_minutes=int(os.environ.get("LOGIN_WINDOW_MINUTES", "15")),
        ),
    )
