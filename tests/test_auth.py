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
