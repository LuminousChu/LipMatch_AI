"""密碼雜湊與登入權杖。

- 密碼：標準函式庫 scrypt（n=2^14, r=8, p=1），每個密碼獨立 16 bytes salt。
- 權杖：隨機 32 bytes，只回給使用者一次；資料庫只存 SHA-256。
- 角色只從資料庫的 users.role 取得，不讀任何請求欄位或標頭。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

_N, _R, _P, _DKLEN = 2**14, 8, 1, 32
MIN_PASSWORD_LENGTH = 12


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"密碼至少 {MIN_PASSWORD_LENGTH} 個字元")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# 帳號不存在時也跑一次雜湊，讓「帳號不存在」與「密碼錯誤」耗時相近
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def burn_password_check(password: str) -> None:
    verify_password(password, _DUMMY_HASH)


def new_token() -> tuple[str, str]:
    """回傳 (給使用者的權杖, 存進資料庫的 SHA-256)。"""
    token = secrets.token_urlsafe(32)
    return token, token_digest(token)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# CSRF 權杖衍生用的固定標籤；改版時換標籤即可讓舊 CSRF 權杖全部失效
_CSRF_LABEL = b"lipmatch-csrf-v1"


# 由 session 權杖衍生出這個 session 專屬的 CSRF 權杖（HMAC-SHA256）。
# 用途：登入與 /api/me 回傳給前端；會修改資料的請求必須在 X-CSRF-Token 標頭帶回，後端重新計算比對。
def csrf_token_for(session_token: str) -> str:
    return hmac.new(session_token.encode("utf-8"), _CSRF_LABEL, hashlib.sha256).hexdigest()
# 核心邏輯：
# 1. 以 session 權杖當 HMAC 金鑰，所以 CSRF 權杖和 session 綁定，換 session 就換 CSRF 權杖。
# 2. 不需要另存資料庫：後端每次從 cookie 拿到 session 權杖後重新計算即可。
# 3. 跨站攻擊者讀不到 HttpOnly cookie，也讀不到同源 API 的回應，因此算不出、拿不到這個值。


# 以固定時間比較用戶端送來的 CSRF 權杖。
# 用途：避免逐字比較造成的時間差洩漏。
def csrf_token_matches(session_token: str, provided: str) -> bool:
    return hmac.compare_digest(csrf_token_for(session_token), provided)
# 核心邏輯：compare_digest 的耗時與內容是否相符無關；內容或長度不同時回 False。
