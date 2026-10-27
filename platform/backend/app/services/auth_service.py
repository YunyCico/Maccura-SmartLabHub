"""平台账号与登录会话。

设计取舍（2026-10-27 定）：

- **账号自建、存 SQLite**：不依赖钉钉/LDAP 审批，管理员在平台里就能加人。
  外部身份（钉钉免登）以后可以作为"另一种登录方式"叠加，不影响这套。
- **密码用 scrypt**（Python 标准库 hashlib）：故意不用 bcrypt/argon2 ——
  那两个要装第三方包，而这个平台要在同事机器上纯 Python 跑起来。
  scrypt 的抗暴力能力和它们同级，参数 n=2^14 单次约 40ms，够用且不卡登录。
- **会话用服务端 token 表**（不是 JWT）：能立刻踢下线（删会话即可），
  也不用把密钥放到环境变量里。cookie 里只放随机 token，
  库里只存它的 SHA-256 摘要 —— 数据库被看到也换不出可用的 cookie。
- **cookie 是 HttpOnly + SameSite=Lax**：JS 读不到，跨站请求不带。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.database import connection

# cookie 名，前后端都要用（前端只需知道存在，不读内容）
SESSION_COOKIE = "slh_session"

SESSION_TTL_DAYS = 7
# scrypt 参数：n 是最主要的成本项。2^14 = 16384，单次约 40ms。
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32

MIN_PASSWORD_LENGTH = 8
# 工号可能很短（比如「95」），所以下限放到 2
MIN_USERNAME_LENGTH = 2
MAX_USERNAME_LENGTH = 32

# 头像：前端先裁成 128×128 再上传，存成 data URL（webp/png）。
# 上限 200KB —— 超了说明没压缩，直接拒掉，免得把库撑大。
MAX_AVATAR_BYTES = 200 * 1024
_AVATAR_PREFIXES = ("data:image/webp;base64,", "data:image/png;base64,", "data:image/jpeg;base64,")

ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"
ROLES = (ROLE_ADMIN, ROLE_MEMBER)


class AuthError(ValueError):
    """可以直接展示给用户的认证错误。"""


# ---------------- 密码 ----------------

def hash_password(password: str) -> str:
    """生成 `scrypt$n$r$p$salt$hash` 格式的密码摘要。"""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(f"密码至少 {MIN_PASSWORD_LENGTH} 位")
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N,
        _SCRYPT_R,
        _SCRYPT_P,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    """校验密码；格式不认识或校验出错一律当作不匹配（不抛异常，避免探测）。"""
    try:
        scheme, n_raw, r_raw, p_raw, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n_raw),
            r=int(r_raw),
            p=int(p_raw),
            dklen=len(expected),
        )
    except Exception:  # noqa: BLE001 - 任何解析失败都视为不匹配
        return False
    # 常量时间比较，避免按响应时间猜密码
    return hmac.compare_digest(actual, expected)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


# ---------------- 账号 ----------------

def _row_to_user(row: sqlite3.Row) -> dict[str, Any]:
    """对外暴露的账号视图 —— **绝不包含 password_hash**。"""
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "role": row["role"],
        "active": bool(row["active"]),
        "avatar": row["avatar"] or "",
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"] or "",
    }


def normalize_avatar(avatar: str) -> str:
    """校验头像 data URL。空串表示清空（回退到姓名首字）。"""
    value = (avatar or "").strip()
    if not value:
        return ""
    if not value.startswith(_AVATAR_PREFIXES):
        raise AuthError("头像格式不支持，请上传 PNG/JPG/WEBP 图片")
    if len(value) > MAX_AVATAR_BYTES:
        raise AuthError("头像太大了，请重新选择（会自动压缩到 128×128）")
    return value


def normalize_username(username: str) -> str:
    value = (username or "").strip()
    if len(value) < MIN_USERNAME_LENGTH:
        raise AuthError(f"账号至少 {MIN_USERNAME_LENGTH} 个字符")
    if len(value) > MAX_USERNAME_LENGTH:
        raise AuthError(f"账号最多 {MAX_USERNAME_LENGTH} 个字符")
    if any(ch.isspace() for ch in value):
        raise AuthError("账号不能包含空格")
    return value


def count_users() -> int:
    with connection() as db:
        row = db.execute("SELECT COUNT(*) AS n FROM users").fetchone()
    return int(row["n"] or 0)


def is_initialized() -> bool:
    """平台是否已经建过账号 —— 决定前端进「首次设置」还是「登录页」。"""
    return count_users() > 0


def list_users() -> list[dict[str, Any]]:
    with connection() as db:
        rows = db.execute(
            "SELECT * FROM users ORDER BY (role = 'admin') DESC, created_at ASC"
        ).fetchall()
    return [_row_to_user(row) for row in rows]


def get_user(user_id: int) -> dict[str, Any] | None:
    with connection() as db:
        row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _row_to_user(row) if row else None


def create_user(
    username: str,
    password: str,
    display_name: str = "",
    role: str = ROLE_MEMBER,
    avatar: str = "",
) -> dict[str, Any]:
    """新建账号。用户名重复会给出可读的错误。"""
    name = normalize_username(username)
    if role not in ROLES:
        raise AuthError("角色只能是 admin 或 member")
    display = (display_name or "").strip() or name
    avatar_value = normalize_avatar(avatar)
    password_hash = hash_password(password)
    now = _iso(_now())
    with connection() as db:
        existing = db.execute(
            "SELECT id FROM users WHERE username = ?", (name,)
        ).fetchone()
        if existing:
            raise AuthError(f"账号「{name}」已存在")
        cursor = db.execute(
            """
            INSERT INTO users
                (username, display_name, password_hash, role, active, avatar, created_at)
            VALUES (?, ?, ?, ?, 1, ?, ?)
            """,
            (name, display, password_hash, role, avatar_value, now),
        )
        user_id = int(cursor.lastrowid)
    created = get_user(user_id)
    assert created is not None
    return created


def create_first_admin(username: str, password: str, display_name: str = "") -> dict[str, Any]:
    """首次设置：只在平台还没有任何账号时可用，防止被人抢先接管。"""
    if is_initialized():
        raise AuthError("平台已经初始化过，请直接登录")
    return create_user(username, password, display_name, ROLE_ADMIN)


def authenticate(username: str, password: str) -> dict[str, Any]:
    """校验账号密码，成功返回账号（含 id），失败抛 AuthError。"""
    name = (username or "").strip()
    # 账号不存在也走一次哈希校验，避免用响应时间探测账号是否存在
    with connection() as db:
        row = db.execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone()
    stored = row["password_hash"] if row else hash_password("__nonexistent__")
    ok = verify_password(password or "", stored)
    if not row or not ok:
        raise AuthError("账号或密码不正确")
    if not row["active"]:
        raise AuthError("这个账号已被停用，请联系管理员")
    with connection() as db:
        db.execute(
            "UPDATE users SET last_login_at = ? WHERE id = ?",
            (_iso(_now()), row["id"]),
        )
    return _row_to_user(row)


def set_password(user_id: int, new_password: str) -> None:
    password_hash = hash_password(new_password)
    with connection() as db:
        cursor = db.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (password_hash, user_id),
        )
        if cursor.rowcount == 0:
            raise AuthError("账号不存在")
        # 改完密码把所有会话踢掉（其他设备要重新登录）
        db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def update_user(
    user_id: int,
    *,
    display_name: str | None = None,
    role: str | None = None,
    active: bool | None = None,
    avatar: str | None = None,
) -> dict[str, Any]:
    sets: list[str] = []
    params: list[Any] = []
    if display_name is not None:
        sets.append("display_name = ?")
        params.append(display_name.strip() or "未命名")
    if avatar is not None:
        sets.append("avatar = ?")
        params.append(normalize_avatar(avatar))
    if role is not None:
        if role not in ROLES:
            raise AuthError("角色只能是 admin 或 member")
        sets.append("role = ?")
        params.append(role)
    if active is not None:
        sets.append("active = ?")
        params.append(1 if active else 0)
    if not sets:
        raise AuthError("没有要修改的字段")

    # 不能把最后一个管理员降级或停用，否则平台再也没人能管账号
    if role == ROLE_MEMBER or active is False:
        with connection() as db:
            row = db.execute(
                "SELECT role, active FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if row and row["role"] == ROLE_ADMIN and row["active"]:
                others = db.execute(
                    "SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND active = 1 AND id <> ?",
                    (user_id,),
                ).fetchone()
                if int(others["n"] or 0) == 0:
                    raise AuthError("这是最后一个管理员，不能降级或停用")

    params.append(user_id)
    with connection() as db:
        cursor = db.execute(
            f"UPDATE users SET {', '.join(sets)} WHERE id = ?", tuple(params)
        )
        if cursor.rowcount == 0:
            raise AuthError("账号不存在")
        if active is False:
            db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    updated = get_user(user_id)
    assert updated is not None
    return updated


def update_own_profile(
    user_id: int,
    *,
    display_name: str | None = None,
    avatar: str | None = None,
) -> dict[str, Any]:
    """改自己的显示名称 / 头像。

    单独开一个函数而不是复用 `update_user`：那个带角色和启用状态，
    绝不能让它被用来提权。这里只允许动显示名的头像。
    """
    sets: list[str] = []
    params: list[Any] = []
    if display_name is not None:
        sets.append("display_name = ?")
        params.append(display_name.strip() or "未命名")
    if avatar is not None:
        sets.append("avatar = ?")
        params.append(normalize_avatar(avatar))
    if not sets:
        raise AuthError("没有要修改的字段")
    params.append(user_id)
    with connection() as db:
        cursor = db.execute(
            f"UPDATE users SET {', '.join(sets)} WHERE id = ?", tuple(params)
        )
        if cursor.rowcount == 0:
            raise AuthError("账号不存在")
    updated = get_user(user_id)
    assert updated is not None
    return updated


def delete_user(user_id: int) -> None:
    with connection() as db:
        row = db.execute(
            "SELECT role, active FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not row:
            raise AuthError("账号不存在")
        if row["role"] == ROLE_ADMIN and row["active"]:
            others = db.execute(
                "SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND active = 1 AND id <> ?",
                (user_id,),
            ).fetchone()
            if int(others["n"] or 0) == 0:
                raise AuthError("这是最后一个管理员，不能删除")
        db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        db.execute("DELETE FROM users WHERE id = ?", (user_id,))


# ---------------- 会话 ----------------

def create_session(user_id: int, user_agent: str = "") -> str:
    """签发登录令牌，返回明文 token（只在这一刻存在于内存/cookie 里）。"""
    token = secrets.token_urlsafe(32)
    now = _now()
    expires = now + timedelta(days=SESSION_TTL_DAYS)
    with connection() as db:
        # 顺手清掉过期会话，省得表越长越大
        db.execute("DELETE FROM sessions WHERE expires_at < ?", (_iso(now),))
        db.execute(
            """
            INSERT INTO sessions (token_hash, user_id, created_at, expires_at, user_agent)
            VALUES (?, ?, ?, ?, ?)
            """,
            (_token_hash(token), user_id, _iso(now), _iso(expires), (user_agent or "")[:200]),
        )
    return token


def resolve_session(token: str) -> dict[str, Any] | None:
    """用 cookie 里的 token 换出账号；过期/停用/不存在都返回 None。"""
    if not token:
        return None
    with connection() as db:
        row = db.execute(
            """
            SELECT u.*, s.expires_at
            FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ?
            """,
            (_token_hash(token),),
        ).fetchone()
        if not row:
            return None
        if row["expires_at"] < _iso(_now()):
            db.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
            return None
        if not row["active"]:
            return None
    return _row_to_user(row)


def delete_session(token: str) -> None:
    if not token:
        return
    with connection() as db:
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))


def session_ttl_seconds() -> int:
    return SESSION_TTL_DAYS * 24 * 60 * 60


# ---------------- 登录失败限流 ----------------
#
# 公网部署后登录接口一定会被人扫。这里做两层限制，都是内存计数（重启即清零）：
#   - 同一个账号：10 分钟内失败 10 次就暂时锁住，防定向爆破
#   - 同一个来源 IP：10 分钟内失败 30 次就暂时锁住，防换账号撞库
# 计数只记失败，成功即清 —— 正常用户输错一两次不会被影响。
#
# 为什么不做持久化：这是"止血"措施，不是审计。
# 真要查谁在爆破，应该看反向代理的访问日志。

_login_failures: dict[str, list[float]] = {}

_FAIL_WINDOW_SECONDS = 600
_MAX_FAILURES_PER_USER = 10
_MAX_FAILURES_PER_IP = 30


def _prune(bucket: list[float], now: float) -> list[float]:
    return [stamp for stamp in bucket if now - stamp < _FAIL_WINDOW_SECONDS]


def _count(key: str, now: float) -> int:
    bucket = _prune(_login_failures.get(key, []), now)
    if bucket:
        _login_failures[key] = bucket
    else:
        _login_failures.pop(key, None)
    return len(bucket)


def login_blocked_reason(client_ip: str, username: str) -> str:
    """返回锁定原因；没被锁返回空串。"""
    now = time.time()
    if _count(f"ip:{client_ip}", now) >= _MAX_FAILURES_PER_IP:
        return "登录尝试过于频繁，请 10 分钟后再试"
    if _count(f"user:{(username or '').strip().lower()}", now) >= _MAX_FAILURES_PER_USER:
        return "这个账号刚才输错太多次，请 10 分钟后再试"
    return ""


def register_login_failure(client_ip: str, username: str) -> None:
    now = time.time()
    for key in (f"ip:{client_ip}", f"user:{(username or '').strip().lower()}"):
        _login_failures.setdefault(key, []).append(now)


def clear_login_failures(client_ip: str, username: str) -> None:
    _login_failures.pop(f"user:{(username or '').strip().lower()}", None)
    # IP 计数不清：同一个人登录成功不代表这个 IP 就不用盯着了


def reset_login_failures() -> None:
    """测试用：清空所有计数。"""
    _login_failures.clear()
