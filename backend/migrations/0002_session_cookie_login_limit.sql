-- 0002_session_cookie_login_limit：DEC-10 認證改為 session cookie＋CSRF，並加入登入失敗限制。
-- 已套用的遷移檔不得修改；要改結構請新增 0003_xxx.sql。欄位說明見 docs/mvp/schema.md。

-- 認證方式從 Bearer 權杖改為 cookie：舊權杖一律撤銷，使用者需重新登入。
-- auth_sessions 結構不變：cookie 裡的 session ID 同樣只存 SHA-256。
UPDATE auth_sessions SET revoked_at = now() WHERE revoked_at IS NULL;

-- 登入嘗試紀錄：只用於計算連續失敗與來源 IP 失敗次數，不存密碼或任何請求內容。
-- 帳號不存在也照樣記錄，回應與限制方式和帳號存在時相同，不透露帳號是否存在。
CREATE TABLE auth_login_attempts (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username      TEXT NOT NULL CHECK (length(username) BETWEEN 1 AND 64),
    client_ip     TEXT NOT NULL CHECK (length(client_ip) BETWEEN 1 AND 64),
    succeeded     BOOLEAN NOT NULL,
    attempted_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX auth_login_attempts_username_idx ON auth_login_attempts (username, attempted_at);
CREATE INDEX auth_login_attempts_ip_idx ON auth_login_attempts (client_ip, attempted_at);
CREATE INDEX auth_login_attempts_time_idx ON auth_login_attempts (attempted_at);
