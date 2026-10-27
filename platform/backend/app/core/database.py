from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3


# 平台自己的库（导入批次、钉钉事件、账号、会话）。
# SLH_AUTH_DB 仅供自动化验证/测试把库指到临时目录 ——
# 否则跑一遍 UI 验证就会往真实库里塞测试账号。
_DEFAULT_DB = Path(__file__).resolve().parents[2] / "department_platform.db"
DATABASE_PATH = Path(os.environ.get("SLH_AUTH_DB") or _DEFAULT_DB)


@contextmanager
def connection() -> Generator[sqlite3.Connection, None, None]:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(DATABASE_PATH)
    database.row_factory = sqlite3.Row
    try:
        ensure_schema(database)
        yield database
        database.commit()
    finally:
        database.close()


def ensure_schema(database: sqlite3.Connection) -> None:
    database.execute(
        """
        CREATE TABLE IF NOT EXISTS import_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_filename TEXT NOT NULL,
            stored_filename TEXT NOT NULL,
            stored_path TEXT NOT NULL,
            total_rows INTEGER NOT NULL,
            sheet_count INTEGER NOT NULL,
            imported_by TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    # 钉钉日程的创建人：靠 Stream 模式接收"日程变更"事件推送记录下来。
    # 钉钉开放接口不返回每条日程的创建人（只有 organizer 和 createTime），
    # 事件推送的 operator 字段是唯一来源。
    database.execute(
        """
        CREATE TABLE IF NOT EXISTS dingtalk_event_creator (
            event_id TEXT PRIMARY KEY,
            calendar_id TEXT,
            change_type TEXT,
            operator_name TEXT,
            operator_userid TEXT,
            operator_union_id TEXT,
            event_time TEXT,
            recorded_at TEXT NOT NULL
        )
        """
    )
    # 平台账号：用户名唯一（不区分大小写），密码只存 scrypt 哈希。
    # role: admin（能管账号/改集成配置/删数据） | member（只能用业务功能）
    database.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            display_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'member',
            active INTEGER NOT NULL DEFAULT 1,
            avatar TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            last_login_at TEXT
        )
        """
    )
    # 老库补字段：CREATE TABLE IF NOT EXISTS 不会给已存在的表加列，
    # 所以头像这列要单独 ALTER 一次（幂等）。
    _columns = {row[1] for row in database.execute("PRAGMA table_info(users)")}
    if "avatar" not in _columns:
        database.execute("ALTER TABLE users ADD COLUMN avatar TEXT NOT NULL DEFAULT ''")
    # 登录会话：只存令牌的 SHA-256 摘要，库被看到也拿不到可用的 cookie。
    database.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            user_agent TEXT
        )
        """
    )
    database.execute(
        "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)"
    )
    database.execute(
        "CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at)"
    )


def initialize_database() -> None:
    with connection():
        pass


def create_import_batch(record: dict[str, object]) -> dict[str, object]:
    with connection() as database:
        cursor = database.execute(
            """
            INSERT INTO import_batches
                (original_filename, stored_filename, stored_path, total_rows, sheet_count, imported_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """ ,
            (
                record["original_filename"],
                record["stored_filename"],
                record["stored_path"],
                record["total_rows"],
                record["sheet_count"],
                record["imported_by"],
                record["created_at"],
            ),
        )
        return {"id": cursor.lastrowid, **record}


def list_import_batches() -> list[dict[str, object]]:
    with connection() as database:
        rows = database.execute(
            "SELECT * FROM import_batches ORDER BY id DESC LIMIT 20"
        ).fetchall()
    return [dict(row) for row in rows]


def record_event_creator(
    event_id: str,
    *,
    calendar_id: str = "",
    change_type: str = "",
    operator_name: str = "",
    operator_userid: str = "",
    operator_union_id: str = "",
    event_time: str = "",
) -> None:
    """记录一条日程的创建人（来自钉钉事件推送）。"""
    if not event_id:
        return
    with connection() as database:
        database.execute(
            """
            INSERT INTO dingtalk_event_creator
                (event_id, calendar_id, change_type, operator_name,
                 operator_userid, operator_union_id, event_time, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                calendar_id = excluded.calendar_id,
                change_type = excluded.change_type,
                operator_name = excluded.operator_name,
                operator_userid = excluded.operator_userid,
                operator_union_id = excluded.operator_union_id,
                event_time = excluded.event_time,
                recorded_at = excluded.recorded_at
            """,
            (
                event_id,
                calendar_id,
                change_type,
                operator_name,
                operator_userid,
                operator_union_id,
                event_time,
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def list_event_creators(event_ids: list[str]) -> dict[str, dict[str, object]]:
    """按日程 id 取已记录的创建人。"""
    if not event_ids:
        return {}
    result: dict[str, dict[str, object]] = {}
    with connection() as database:
        for start in range(0, len(event_ids), 400):
            chunk = event_ids[start:start + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows = database.execute(
                f"SELECT * FROM dingtalk_event_creator WHERE event_id IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in rows:
                result[str(row["event_id"])] = dict(row)
    return result


def count_event_creators() -> int:
    with connection() as database:
        row = database.execute("SELECT COUNT(*) AS total FROM dingtalk_event_creator").fetchone()
    return int(row["total"]) if row else 0
