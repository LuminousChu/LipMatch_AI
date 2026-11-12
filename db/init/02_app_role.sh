#!/bin/sh
# 建立後端專用的非超級使用者帳號（資訊安全規格 v0.1 §2「應用 DB 帳號非超級使用者」）。
# 用途：
#   - volume 第一次初始化時由 docker-entrypoint-initdb.d 自動執行；
#   - 既有 volume 可手動補跑：docker compose exec db sh /docker-entrypoint-initdb.d/02_app_role.sh
# 可重複執行：帳號已存在時只更新密碼與權限。
set -eu

: "${APP_DB_USER:?APP_DB_USER 未設定}"
: "${APP_DB_PASSWORD:?APP_DB_PASSWORD 未設定}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v app_user="$APP_DB_USER" -v app_password="$APP_DB_PASSWORD" -v db_name="$POSTGRES_DB" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS', :'app_user')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_user') \gexec
SELECT format('ALTER ROLE %I WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
              :'app_user', :'app_password') \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', :'db_name', :'app_user') \gexec
SELECT format('GRANT USAGE, CREATE ON SCHEMA public TO %I', :'app_user') \gexec
SQL
# 核心邏輯：
# 1. 帳號名稱與密碼以 psql 變數傳入，再用 format(%I, %L) 產生 SQL：
#    %I 會把名稱當識別字加引號、%L 會把密碼當字串常值跳脫，不做 shell 字串拼接。
# 2. \gexec 會執行上一個查詢回傳的 SQL；帳號不存在才 CREATE，之後一律 ALTER，
#    所以重跑時能同步 .env 的新密碼，也會把權限修回「非超級使用者」。
# 3. 只給連線資料庫、在 public schema 使用與建表的權限；
#    pgvector 擴充由 01_extensions.sql 以管理帳號建立，後端帳號不需要建立擴充的權限。
