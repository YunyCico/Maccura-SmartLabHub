"""平台账号体系测试。

覆盖：
- 首次设置只能成功一次（防止被人抢先接管）
- 密码用 scrypt 存储，库里看不到明文
- 登录后能访问业务接口；未登录一律 401
- 普通成员碰不到管理员接口（403）
- 改密码后旧会话失效
- 不能删/降级最后一个管理员（否则平台没人能管）
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "platform" / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


@pytest.fixture()
def client(monkeypatch, tmp_path: Path):
    """把账号库指到临时文件，测完不污染真实账号。"""
    from app.core import database

    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "auth_test.db")

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


def _setup_admin(client, username="admin", password="Passw0rd!x"):
    return client.post(
        "/api/auth/setup",
        json={"username": username, "password": password, "display_name": "管理员"},
    )


# ---------------- 首次设置 ----------------

def test_status_reports_uninitialized_before_setup(client) -> None:
    body = client.get("/api/auth/status").json()
    assert body == {"initialized": False, "user": None}


def test_setup_creates_admin_and_logs_in(client) -> None:
    resp = _setup_admin(client)
    assert resp.status_code == 200
    user = resp.json()["user"]
    assert user["role"] == "admin"
    assert user["username"] == "admin"
    # 建完直接就是登录态
    assert client.get("/api/auth/status").json()["initialized"] is True
    assert client.get("/api/auth/me").json()["user"]["username"] == "admin"


def test_setup_can_only_run_once(client) -> None:
    assert _setup_admin(client).status_code == 200
    again = _setup_admin(client, username="hacker", password="Hacker123!")
    assert again.status_code == 400
    assert "已经初始化" in again.json()["detail"]


def test_setup_rejects_short_password(client) -> None:
    resp = client.post(
        "/api/auth/setup", json={"username": "admin", "password": "123"}
    )
    assert resp.status_code == 400
    assert "至少" in resp.json()["detail"]


# ---------------- 密码存储 ----------------

def test_password_is_scrypt_and_never_returned(client, tmp_path: Path) -> None:
    _setup_admin(client)
    from app.core import database

    conn = sqlite3.connect(database.DATABASE_PATH)
    stored = conn.execute("SELECT password_hash FROM users").fetchone()[0]
    conn.close()
    assert stored.startswith("scrypt$")
    assert "Passw0rd!x" not in stored
    # 接口回包里也绝不能有 hash
    body = client.get("/api/auth/users").json()
    assert "password" not in str(body)


# ---------------- 登录与守卫 ----------------

def test_protected_api_requires_login(client) -> None:
    _setup_admin(client)
    client.cookies.clear()
    resp = client.get("/api/dashboard/summary")
    assert resp.status_code == 401
    assert resp.json()["need_login"] is True


def test_public_endpoints_stay_open(client) -> None:
    """启动器靠 health / embed-status 判断就绪，登录前也必须能访问。"""
    client.cookies.clear()
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/data-analysis/embed-status").status_code == 200
    assert client.get("/api/dingtalk/config").status_code == 200
    assert client.get("/api/auth/status").status_code == 200


def test_login_with_wrong_password_is_rejected(client) -> None:
    _setup_admin(client)
    client.cookies.clear()
    resp = client.post(
        "/api/auth/login", json={"username": "admin", "password": "wrong-password"}
    )
    assert resp.status_code == 401
    assert "账号或密码不正确" in resp.json()["detail"]


def test_login_with_correct_password_works(client) -> None:
    _setup_admin(client, username="yun", password="YunPass123!")
    client.cookies.clear()
    resp = client.post(
        "/api/auth/login", json={"username": "yun", "password": "YunPass123!"}
    )
    assert resp.status_code == 200
    assert resp.json()["user"]["username"] == "yun"
    assert client.get("/api/auth/me").status_code == 200


def test_username_is_case_insensitive(client) -> None:
    _setup_admin(client, username="Yun", password="YunPass123!")
    client.cookies.clear()
    resp = client.post(
        "/api/auth/login", json={"username": "yun", "password": "YunPass123!"}
    )
    assert resp.status_code == 200


def test_logout_clears_session(client) -> None:
    _setup_admin(client)
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/dashboard/summary").status_code == 401


# ---------------- 角色 ----------------

def test_member_cannot_manage_users(client) -> None:
    _setup_admin(client)
    created = client.post(
        "/api/auth/users",
        json={"username": "member1", "password": "Member123!", "role": "member"},
    )
    assert created.status_code == 200

    client.cookies.clear()
    login = client.post(
        "/api/auth/login", json={"username": "member1", "password": "Member123!"}
    )
    assert login.status_code == 200
    assert login.json()["user"]["role"] == "member"
    # 业务接口能用
    assert client.get("/api/dashboard/summary").status_code == 200
    # 管理接口不行
    assert client.get("/api/auth/users").status_code == 403


def test_admin_can_reset_other_password(client) -> None:
    _setup_admin(client)
    created = client.post(
        "/api/auth/users",
        json={"username": "member1", "password": "Member123!", "role": "member"},
    ).json()["user"]

    resp = client.post(
        f"/api/auth/users/{created['id']}/password",
        json={"new_password": "NewPass456!"},
    )
    assert resp.status_code == 200

    client.cookies.clear()
    assert client.post(
        "/api/auth/login", json={"username": "member1", "password": "Member123!"}
    ).status_code == 401
    assert client.post(
        "/api/auth/login", json={"username": "member1", "password": "NewPass456!"}
    ).status_code == 200


# ---------------- 自我保护 ----------------

def test_cannot_delete_last_admin(client) -> None:
    admin = _setup_admin(client).json()["user"]
    resp = client.request("DELETE", f"/api/auth/users/{admin['id']}")
    # 自己删自己先被挡
    assert resp.status_code == 400


def test_cannot_demote_last_admin(client) -> None:
    admin = _setup_admin(client).json()["user"]
    resp = client.patch(f"/api/auth/users/{admin['id']}", json={"role": "member"})
    assert resp.status_code == 400
    assert "最后一个管理员" in resp.json()["detail"]


def test_can_demote_admin_when_another_exists(client) -> None:
    admin = _setup_admin(client).json()["user"]
    client.post(
        "/api/auth/users",
        json={"username": "admin2", "password": "Admin2pass!", "role": "admin"},
    )
    resp = client.patch(f"/api/auth/users/{admin['id']}", json={"role": "member"})
    assert resp.status_code == 200
    assert resp.json()["user"]["role"] == "member"


def test_deactivated_user_is_kicked_out(client) -> None:
    _setup_admin(client)
    created = client.post(
        "/api/auth/users",
        json={"username": "member1", "password": "Member123!", "role": "member"},
    ).json()["user"]

    # 用另一套 cookie 模拟成员登录
    member = client.__class__(client.app)
    member.post("/api/auth/login", json={"username": "member1", "password": "Member123!"})
    assert member.get("/api/dashboard/summary").status_code == 200

    # 管理员停用他
    assert client.patch(
        f"/api/auth/users/{created['id']}", json={"active": False}
    ).status_code == 200
    assert member.get("/api/dashboard/summary").status_code == 401


# ---------------- 改自己的密码 ----------------

def test_change_own_password_invalidates_sessions(client) -> None:
    _setup_admin(client)
    resp = client.post(
        "/api/auth/password",
        json={"old_password": "Passw0rd!x", "new_password": "Brand2New!"},
    )
    assert resp.status_code == 200
    # 改完自己的会话也被清掉，要重新登录
    assert client.get("/api/dashboard/summary").status_code == 401
    assert client.post(
        "/api/auth/login", json={"username": "admin", "password": "Brand2New!"}
    ).status_code == 200


def test_change_password_requires_correct_old_password(client) -> None:
    _setup_admin(client)
    resp = client.post(
        "/api/auth/password",
        json={"old_password": "not-the-real-one", "new_password": "Whatever123!"},
    )
    assert resp.status_code == 400


# ---------------- 工号长度 ----------------

def test_short_employee_id_is_allowed(client) -> None:
    """工号可能只有 2 位（比如 95），不能被「至少 3 位」挡住。"""
    resp = client.post(
        "/api/auth/setup",
        json={"username": "95", "password": "Passw0rd!x", "display_name": "95"},
    )
    assert resp.status_code == 200
    assert resp.json()["user"]["username"] == "95"
    client.cookies.clear()
    assert client.post(
        "/api/auth/login", json={"username": "95", "password": "Passw0rd!x"}
    ).status_code == 200


# ---------------- 名称 / 头像 ----------------

_AVATAR = "data:image/webp;base64,AAAA"


def test_update_own_display_name_and_avatar(client) -> None:
    """自己可以改显示名和头像，改完 /me 立刻是新的。"""
    _setup_admin(client, username="95", password="Passw0rd!x")
    resp = client.patch(
        "/api/auth/profile",
        json={"display_name": "孟鹏杰", "avatar": _AVATAR},
    )
    assert resp.status_code == 200
    user = resp.json()["user"]
    assert user["display_name"] == "孟鹏杰"
    assert user["avatar"] == _AVATAR
    # 再查一次，确认落了库
    me = client.get("/api/auth/me").json()["user"]
    assert me["display_name"] == "孟鹏杰"
    assert me["avatar"] == _AVATAR


def test_clear_avatar_falls_back_to_initials(client) -> None:
    """头像传空串 = 清除，前端会用姓名首字兜底。"""
    _setup_admin(client)
    client.patch("/api/auth/profile", json={"avatar": _AVATAR})
    resp = client.patch("/api/auth/profile", json={"avatar": ""})
    assert resp.status_code == 200
    assert resp.json()["user"]["avatar"] == ""


def test_avatar_rejects_non_image(client) -> None:
    """不是图片的 data URL 要拒掉（防止往里塞任意内容）。"""
    _setup_admin(client)
    resp = client.patch(
        "/api/auth/profile",
        json={"avatar": "data:text/html;base64,PHNjcmlwdD4="},
    )
    assert resp.status_code == 400
    assert "格式不支持" in resp.json()["detail"]


def test_avatar_rejects_oversized(client) -> None:
    """超过 200KB 的头像要拒掉（说明没在前端压缩）。"""
    _setup_admin(client)
    huge = "data:image/png;base64," + "A" * (201 * 1024)
    resp = client.patch("/api/auth/profile", json={"avatar": huge})
    assert resp.status_code == 400
    assert "太大" in resp.json()["detail"]


def test_profile_cannot_escalate_role(client) -> None:
    """改资料接口不能用来提权：多传 role 不生效。"""
    _setup_admin(client)
    client.post(
        "/api/auth/users",
        json={"username": "member1", "password": "Member123!", "role": "member"},
    )
    client.cookies.clear()
    client.post("/api/auth/login", json={"username": "member1", "password": "Member123!"})

    resp = client.patch(
        "/api/auth/profile",
        json={"display_name": "想提权", "role": "admin", "active": True},
    )
    assert resp.status_code == 200
    assert resp.json()["user"]["role"] == "member"
    assert resp.json()["user"]["display_name"] == "想提权"


def test_admin_can_edit_other_user_profile(client) -> None:
    """管理员能改别人的名称和头像。"""
    _setup_admin(client)
    created = client.post(
        "/api/auth/users",
        json={"username": "8873", "password": "Member123!", "role": "member"},
    ).json()["user"]

    resp = client.patch(
        f"/api/auth/users/{created['id']}",
        json={"display_name": "张三", "avatar": _AVATAR},
    )
    assert resp.status_code == 200
    assert resp.json()["user"]["display_name"] == "张三"
    assert resp.json()["user"]["avatar"] == _AVATAR

    listed = client.get("/api/auth/users").json()["users"]
    target = next(u for u in listed if u["id"] == created["id"])
    assert target["display_name"] == "张三"
    assert target["avatar"] == _AVATAR


def test_create_user_with_avatar(client) -> None:
    """建号时可以直接带头像。"""
    _setup_admin(client)
    resp = client.post(
        "/api/auth/users",
        json={
            "username": "12121",
            "password": "Member123!",
            "display_name": "李四",
            "role": "admin",
            "avatar": _AVATAR,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["user"]["avatar"] == _AVATAR


def test_member_cannot_edit_others_profile(client) -> None:
    """普通成员不能改别人的资料。"""
    _setup_admin(client)
    created = client.post(
        "/api/auth/users",
        json={"username": "member1", "password": "Member123!", "role": "member"},
    ).json()["user"]
    client.cookies.clear()
    client.post("/api/auth/login", json={"username": "member1", "password": "Member123!"})
    resp = client.patch(
        f"/api/auth/users/{created['id']}", json={"display_name": "越权"}
    )
    assert resp.status_code == 403

# ---------------- 对外发布：引擎反代 / 文档开关 / 登录限流 ----------------

def test_engine_proxy_requires_login(client) -> None:
    """引擎原来只绑本机、接口裸奔；挂到 /engine 之后同样要登录。"""
    _setup_admin(client)
    client.cookies.clear()
    assert client.get("/engine/").status_code == 401


def test_engine_proxy_serves_workbench_when_logged_in(client) -> None:
    """登录后 /engine/ 能拿到工作台页面（不是 502/503）。"""
    _setup_admin(client)
    resp = client.get("/engine/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "maccura" in resp.text.lower() or "SmartLabHub" in resp.text


def test_engine_proxy_forwards_api(client) -> None:
    """引擎自己的 /api/ 也要能经代理走通（前端靠它取配置）。"""
    _setup_admin(client)
    resp = client.get("/engine/api/state")
    assert resp.status_code == 200
    assert resp.json().get("ok") is True


def test_embed_url_is_relative_not_localhost(client) -> None:
    """iframe 地址必须是 /engine/ 这种相对路径。

    一旦退回 http://127.0.0.1:8765/，同事在自己电脑上打开平台时，
    那个 127.0.0.1 指的是他自己的机器 —— iframe 直接白屏。
    """
    _setup_admin(client)
    status = client.get("/api/data-analysis/embed-status").json()
    assert status["url"].startswith("/engine")
    assert "127.0.0.1" not in status["url"]


def test_login_is_rate_limited(client) -> None:
    """连续输错要限流，否则公网上会被人无限撞库。"""
    from app.services import auth_service

    auth_service.reset_login_failures()
    _setup_admin(client)
    client.cookies.clear()

    codes = []
    for _ in range(12):
        codes.append(
            client.post(
                "/api/auth/login", json={"username": "admin", "password": "wrong"}
            ).status_code
        )
    assert 429 in codes, f"一直没被限流，返回码={codes}"
    blocked = client.post(
        "/api/auth/login", json={"username": "admin", "password": "Passw0rd!x"}
    )
    assert blocked.status_code == 429
    assert "分钟后再试" in blocked.json()["detail"]
    auth_service.reset_login_failures()


def test_login_failure_counter_clears_on_success(client) -> None:
    """成功登录后该账号的失败计数清零，正常用户输错一两次不受影响。"""
    from app.services import auth_service

    auth_service.reset_login_failures()
    _setup_admin(client)
    client.cookies.clear()
    for _ in range(3):
        client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert client.post(
        "/api/auth/login", json={"username": "admin", "password": "Passw0rd!x"}
    ).status_code == 200
    client.cookies.clear()
    assert client.post(
        "/api/auth/login", json={"username": "admin", "password": "Passw0rd!x"}
    ).status_code == 200
    auth_service.reset_login_failures()


# ---------------- 界面偏好（Notifications Center 区块排序）----------------

def test_prefs_require_login(client) -> None:
    """未登录不能读写偏好 —— 中间件白名单放了 /api/auth/，这里要自己兜住。"""
    _setup_admin(client)
    anonymous = type(client)(client.app)
    assert anonymous.get("/api/auth/prefs").status_code == 401
    assert anonymous.patch("/api/auth/prefs", json={"nc": {}}).status_code == 401


def test_prefs_default_is_empty_object(client) -> None:
    """从没设置过时返回空对象，前端据此用默认顺序。"""
    _setup_admin(client)
    response = client.get("/api/auth/prefs")
    assert response.status_code == 200
    assert response.json()["prefs"] == {}


def test_prefs_roundtrip(client) -> None:
    """存进去什么就能原样读回来（后端不解释内容）。"""
    _setup_admin(client)
    payload = {
        "nc": {
            "sources": ["oa", "dingtalk", "salesforce"],
            "kinds": {"dingtalk": ["trip", "todo", "calendar"]},
        }
    }
    assert client.patch("/api/auth/prefs", json=payload).status_code == 200
    assert client.get("/api/auth/prefs").json()["prefs"] == payload


def test_prefs_save_is_full_overwrite(client) -> None:
    """整体覆盖：删掉一个键后再读就真的没了（不做深合并）。"""
    _setup_admin(client)
    client.patch("/api/auth/prefs", json={"nc": {"sources": ["oa"]}, "other": 1})
    client.patch("/api/auth/prefs", json={"nc": {"sources": ["salesforce"]}})
    prefs = client.get("/api/auth/prefs").json()["prefs"]
    assert prefs == {"nc": {"sources": ["salesforce"]}}
    assert "other" not in prefs


def test_prefs_rejects_oversized_payload(client) -> None:
    """偏好只是展示设置，不该被拿来当存储用。"""
    _setup_admin(client)
    response = client.patch("/api/auth/prefs", json={"junk": "x" * 9000})
    assert response.status_code == 400


def test_prefs_are_per_user(client, tmp_path: Path) -> None:
    """A 的排序不影响 B —— 这是"跟账号走"的核心。"""
    from app.services import auth_service

    _setup_admin(client)
    admin_cookies = dict(client.cookies)
    client.patch("/api/auth/prefs", json={"nc": {"sources": ["oa"]}})

    auth_service.create_user("member1", "Passw0rd!x", "成员一")
    client.cookies.clear()
    assert client.post(
        "/api/auth/login", json={"username": "member1", "password": "Passw0rd!x"}
    ).status_code == 200
    # 成员一没设过，应该是空
    assert client.get("/api/auth/prefs").json()["prefs"] == {}
    client.patch("/api/auth/prefs", json={"nc": {"sources": ["salesforce"]}})

    # 换回管理员，自己那份没被改
    client.cookies.clear()
    client.cookies.update(admin_cookies)
    assert client.get("/api/auth/prefs").json()["prefs"] == {"nc": {"sources": ["oa"]}}


def test_ui_prefs_column_added_to_legacy_db(tmp_path: Path, monkeypatch) -> None:
    """老库（没有 ui_prefs 列）打开后要自动补列，不能报错。"""
    import sqlite3 as _sqlite

    from app.core import database

    legacy = tmp_path / "legacy.db"
    conn = _sqlite.connect(legacy)
    conn.execute(
        """
        CREATE TABLE users (
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
    conn.execute(
        "INSERT INTO users (username, display_name, password_hash, role, active, created_at) "
        "VALUES ('old', '老账号', 'scrypt$x', 'admin', 1, '2026-01-01')"
    )
    conn.commit()
    conn.close()

    # 把模块级路径指到这份老库，connection() 一打开就会跑 ensure_schema 补列
    monkeypatch.setattr(database, "DATABASE_PATH", legacy)
    with database.connection() as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
        assert "ui_prefs" in columns
        # 老数据还在，且新列默认空串（等价于"没设置过"）
        row = db.execute("SELECT username, ui_prefs FROM users").fetchone()
        assert row["username"] == "old"
        assert row["ui_prefs"] == ""
