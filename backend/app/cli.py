"""後端管理指令：遷移與預建帳號（沒有註冊與忘記密碼功能）。

  python -m app.cli migrate
  python -m app.cli create-user --username alice --role user        # 互動輸入密碼兩次
  python -m app.cli create-user --username ops --role admin --password-stdin < 密碼檔
  python -m app.cli set-password --username alice
  python -m app.cli deactivate --username alice

密碼不可放在指令列參數（會留在 shell 歷史與程序清單）。
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

import psycopg

from .migrate import migrate
from .security import MIN_PASSWORD_LENGTH, hash_password


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("請設定 DATABASE_URL")
    return url


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("密碼：")
        if getpass.getpass("再輸入一次：") != password:
            sys.exit("兩次輸入的密碼不同")
    if len(password) < MIN_PASSWORD_LENGTH:
        sys.exit(f"密碼至少 {MIN_PASSWORD_LENGTH} 個字元")
    return password


def create_user(database_url: str, username: str, role: str, password: str) -> int:
    with psycopg.connect(database_url) as conn:
        row = conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (%s, %s, %s) RETURNING id",
            (username.strip().lower(), hash_password(password), role),
        ).fetchone()
    return row[0]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="套用資料庫遷移")
    p = sub.add_parser("create-user", help="預建帳號")
    p.add_argument("--username", required=True)
    p.add_argument("--role", required=True, choices=["user", "admin"])
    p.add_argument("--password-stdin", action="store_true")
    p = sub.add_parser("set-password", help="重設密碼（並撤銷所有登入）")
    p.add_argument("--username", required=True)
    p.add_argument("--password-stdin", action="store_true")
    p = sub.add_parser("deactivate", help="停用帳號（並撤銷所有登入）")
    p.add_argument("--username", required=True)
    args = parser.parse_args(argv)

    url = _database_url()
    if args.cmd == "migrate":
        applied = migrate(url)
        print("已套用：" + (", ".join(applied) if applied else "無（已是最新）"))
        return
    if args.cmd == "create-user":
        try:
            uid = create_user(url, args.username, args.role, _read_password(args.password_stdin))
        except psycopg.errors.UniqueViolation:
            sys.exit(f"帳號已存在：{args.username}")
        except psycopg.errors.CheckViolation:
            sys.exit("帳號只能用 3–64 個小寫英數字與 _ . -")
        print(f"已建立 {args.role} 帳號 {args.username}（id={uid}）")
        return

    username = args.username.strip().lower()
    with psycopg.connect(url) as conn:
        if args.cmd == "set-password":
            sql, params = "UPDATE users SET password_hash = %s WHERE username = %s RETURNING id", (
                hash_password(_read_password(args.password_stdin)),
                username,
            )
        else:
            sql, params = "UPDATE users SET is_active = FALSE WHERE username = %s RETURNING id", (username,)
        row = conn.execute(sql, params).fetchone()
        if row is None:
            sys.exit(f"找不到帳號：{username}")
        conn.execute(
            "UPDATE auth_sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL", (row[0],)
        )
    print(f"完成：{args.cmd} {username}")


if __name__ == "__main__":
    main()
