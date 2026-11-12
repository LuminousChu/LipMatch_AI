"""SQL 遷移：依檔名順序套用 migrations/NNNN_*.sql，並以 SHA-256 防止已套用的檔案被修改。

用法：python -m app.cli migrate
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
# 任意固定數字：同時只允許一個程序跑遷移
_LOCK_KEY = 0x4C49504D  # "LIPM"


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def load_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    found: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        m = _NAME.match(path.name)
        if not m:
            raise MigrationError(f"遷移檔名不符合 NNNN_name.sql：{path.name}")
        found.append(Migration(m.group(1), path.name, path.read_text(encoding="utf-8")))
    versions = [m.version for m in found]
    if len(versions) != len(set(versions)):
        raise MigrationError(f"遷移版本號重複：{versions}")
    return found


def migrate(database_url: str, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """套用尚未執行的遷移，回傳本次套用的檔名。每個檔案一個交易，失敗則該檔完全不生效。"""
    migrations = load_migrations(directory)
    applied_now: list[str] = []
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                   version     TEXT PRIMARY KEY,
                   name        TEXT NOT NULL,
                   checksum    TEXT NOT NULL,
                   applied_at  TIMESTAMPTZ NOT NULL DEFAULT now())"""
        )
        conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
        try:
            applied = {
                v: (n, c) for v, n, c in conn.execute("SELECT version, name, checksum FROM schema_migrations")
            }
            unknown = set(applied) - {m.version for m in migrations}
            if unknown:
                raise MigrationError(f"資料庫有程式不認得的遷移版本：{sorted(unknown)}")
            for mig in migrations:
                if mig.version in applied:
                    name, checksum = applied[mig.version]
                    if checksum != mig.checksum:
                        raise MigrationError(
                            f"{mig.name} 已套用但內容被修改（checksum 不符）；請改用新的遷移檔"
                        )
                    continue
                with conn.transaction():
                    conn.execute(mig.sql)
                    conn.execute(
                        "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                        (mig.version, mig.name, mig.checksum),
                    )
                applied_now.append(mig.name)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
    return applied_now
