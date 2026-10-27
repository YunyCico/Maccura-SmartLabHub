#!/usr/bin/env python3
"""平台账号迁移工具 —— 把账号（含密码）从一台机器搬到另一台。

背景（2026-10-27）：
账号库 `platform/backend/department_platform.db` 被 `.gitignore` 挡掉了
（它含密码摘要，绝不能进 git）。所以服务器上 `git clone` 之后是**空库**，
本机开好的那批账号不会跟过去，只能重开一遍。

这个脚本解决的就是这件事：
把本机账号**导出成一个 JSON 文件**（人肉拷到服务器），
再在服务器上**导入**进账号库。

⚠️ 安全约定：
- 导出文件里含 scrypt 密码摘要 —— 等同于"密码的等价物"。
  **绝对不要提交到 git、不要发到聊天工具、不要放共享盘**。
  用完即删。
- 会话（sessions 表）**不迁移**：旧 token 在新机器上没必要，
  迁移完每个人重新登录一次更干净。

用法：
    # 在本机导出（默认写到 platform/backend/accounts_export.json）
    python platform/tools/migrate_accounts.py export

    # 指定输出路径
    python platform/tools/migrate_accounts.py export --out /tmp/accounts.json

    # 在服务器上导入（先停服务再导，避免写库冲突）
    python platform/tools/migrate_accounts.py import --file /tmp/accounts.json

    # 只看有什么账号，不导出
    python platform/tools/migrate_accounts.py list
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
DEFAULT_DB = BACKEND_DIR / "department_platform.db"
DEFAULT_OUT = BACKEND_DIR / "accounts_export.json"

# 导出文件的格式版本。以后字段变了就 +1，导入端遇到不认识的版本直接报错。
EXPORT_VERSION = 1

ROLES = ("admin", "member")


def _connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise SystemExit(f"账号库不存在：{db_path}\n先在本机跑一次平台，账号库会自动建出来。")
    return sqlite3.connect(str(db_path))


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """服务器上是空库，users 表可能还没建。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            display_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'member',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_login_at TEXT
        , avatar TEXT NOT NULL DEFAULT '')
        """
    )
    conn.commit()


def cmd_export(args: argparse.Namespace) -> int:
    conn = _connect(Path(args.db))
    _ensure_schema(conn)
    rows = conn.execute(
        "SELECT username, display_name, password_hash, role, active, avatar, created_at "
        "FROM users ORDER BY id"
    ).fetchall()
    conn.close()

    if not rows:
        print("账号库里一个账号都没有，没什么可导出的。")
        return 1

    payload = {
        "version": EXPORT_VERSION,
        "exported_at": _now(),
        "users": [
            {
                "username": r[0],
                "display_name": r[1],
                "password_hash": r[2],
                "role": r[3] or "member",
                "active": int(r[4] or 0),
                "avatar": r[5] or "",
                "created_at": r[6],
            }
            for r in rows
        ],
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        os.chmod(out_path, 0o600)  # POSIX；Windows 上会被忽略
    except OSError:
        pass

    admins = sum(1 for u in payload["users"] if u["role"] == "admin")
    print(f"已导出 {len(payload['users'])} 个账号（其中管理员 {admins} 个）→ {out_path}")
    print("⚠️  这个文件含密码摘要，等同于密码。用完请立刻删掉，别提交到 git。")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    src = Path(args.file)
    if not src.is_file():
        raise SystemExit(f"找不到导入文件：{src}")

    payload = json.loads(src.read_text(encoding="utf-8"))
    version = payload.get("version")
    if version != EXPORT_VERSION:
        raise SystemExit(
            f"导出文件版本不匹配：文件是 v{version}，本脚本只认 v{EXPORT_VERSION}。"
            "升级脚本或重新导出。"
        )

    users = payload.get("users") or []
    if not users:
        print("导入文件里没有账号。")
        return 1

    db_path = Path(args.db)
    conn = _connect(db_path) if db_path.is_file() else sqlite3.connect(str(db_path))
    _ensure_schema(conn)

    created, updated, skipped = [], [], []
    for u in users:
        username = (u.get("username") or "").strip()
        if not username:
            skipped.append("(空用户名)")
            continue
        role = u.get("role") if u.get("role") in ROLES else "member"
        display_name = u.get("display_name") or username
        password_hash = u.get("password_hash") or ""
        if not password_hash:
            # 没带摘要就没法迁移这个账号 —— 总比塞个空密码进去强
            skipped.append(f"{username}(无密码摘要)")
            continue
        avatar = u.get("avatar") or ""
        active = 1 if int(u.get("active", 1)) else 0
        created_at = u.get("created_at") or _now()

        exists = conn.execute(
            "SELECT id FROM users WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
        if exists:
            if args.skip_existing:
                skipped.append(f"{username}(已存在)")
                continue
            conn.execute(
                "UPDATE users SET display_name = ?, password_hash = ?, role = ?, "
                "active = ?, avatar = ? WHERE id = ?",
                (display_name, password_hash, role, active, avatar, exists[0]),
            )
            updated.append(username)
        else:
            conn.execute(
                "INSERT INTO users (username, display_name, password_hash, role, active, "
                "created_at, avatar) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (username, display_name, password_hash, role, active, created_at, avatar),
            )
            created.append(username)

    conn.commit()

    # 最后一道保险：不能把最后一个管理员导没了（后端也拦，这里再兜一层）
    admin_count = conn.execute(
        "SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1"
    ).fetchone()[0]
    conn.close()

    print(f"导入完成：新建 {len(created)} 个，更新 {len(updated)} 个，跳过 {len(skipped)} 个")
    if created:
        print("  新建：" + "、".join(created))
    if updated:
        print("  更新：" + "、".join(updated))
    if skipped:
        print("  跳过：" + "、".join(skipped))
    print(f"当前管理员数量：{admin_count}")
    if admin_count == 0:
        print("⚠️  一个管理员都没有了，平台会拒绝登录。建完记得留至少一个 admin。")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    conn = _connect(Path(args.db))
    rows = conn.execute(
        "SELECT username, display_name, role, active, last_login_at FROM users ORDER BY id"
    ).fetchall()
    conn.close()
    if not rows:
        print("账号库是空的。")
        return 0
    print(f"{'工号':<12}{'姓名':<16}{'角色':<8}{'状态':<6}最近登录")
    for r in rows:
        status = "启用" if r[3] else "停用"
        print(f"{r[0]:<12}{r[1]:<16}{r[2]:<8}{status:<6}{r[4] or '从未'}")
    print(f"\n共 {len(rows)} 个账号")
    return 0


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="平台账号迁移工具（导出/导入/查看）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_exp = sub.add_parser("export", help="导出账号到 JSON")
    p_exp.add_argument("--db", default=str(DEFAULT_DB), help=f"账号库路径（默认 {DEFAULT_DB.name}）")
    p_exp.add_argument("--out", default=str(DEFAULT_OUT), help=f"导出文件路径（默认 {DEFAULT_OUT.name}）")
    p_exp.set_defaults(func=cmd_export)

    p_imp = sub.add_parser("import", help="从 JSON 导入账号")
    p_imp.add_argument("--db", default=str(DEFAULT_DB), help=f"账号库路径（默认 {DEFAULT_DB.name}）")
    p_imp.add_argument("--file", required=True, help="导入文件路径")
    p_imp.add_argument(
        "--skip-existing",
        action="store_true",
        help="已存在的账号不覆盖（默认覆盖：更新姓名/角色/密码）",
    )
    p_imp.set_defaults(func=cmd_import)

    p_ls = sub.add_parser("list", help="列出账号")
    p_ls.add_argument("--db", default=str(DEFAULT_DB), help=f"账号库路径（默认 {DEFAULT_DB.name}）")
    p_ls.set_defaults(func=cmd_list)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
