"""平台账号接口。

- GET  /api/auth/status     是否已初始化 + 当前登录人（登录页用，公开）
- POST /api/auth/setup      首次创建管理员（只在没有账号时可用，公开）
- POST /api/auth/login      账号密码登录，下发 HttpOnly cookie
- POST /api/auth/logout     退出登录
- GET  /api/auth/me         当前登录人
- POST /api/auth/password   修改自己的密码
- 管理员：GET/POST /api/auth/users、PATCH/DELETE /api/auth/users/{id}
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from app.core.config import get_settings
from app.services import auth_service
from app.services.auth_service import (
    AuthError,
    ROLE_ADMIN,
    SESSION_COOKIE,
    session_ttl_seconds,
)

router = APIRouter(prefix="/auth", tags=["auth"])


class SetupPayload(BaseModel):
    username: str = ""
    password: str = ""
    display_name: str = ""


class LoginPayload(BaseModel):
    username: str = ""
    password: str = ""


class PasswordPayload(BaseModel):
    old_password: str = ""
    new_password: str = ""


class ProfilePayload(BaseModel):
    """改自己的资料：显示名称 / 头像。头像传空串表示清空。"""

    display_name: str | None = None
    avatar: str | None = None


class UserCreatePayload(BaseModel):
    username: str = ""
    password: str = ""
    display_name: str = ""
    role: str = auth_service.ROLE_MEMBER
    avatar: str = ""


class UserUpdatePayload(BaseModel):
    display_name: str | None = None
    role: str | None = None
    active: bool | None = None
    avatar: str | None = None


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=session_ttl_seconds(),
        httponly=True,          # JS 读不到，防 XSS 偷 token
        samesite="lax",         # 跨站请求不带
        # 上 HTTPS 后必须打开，否则 cookie 会以明文经过网络。
        # 由 COOKIE_SECURE 环境变量控制，默认关闭是为了本机 http 调试能用。
        secure=get_settings().cookie_secure,
        path="/",
    )


def _client_ip(request: Request) -> str:
    """取真实来源 IP。

    放在反向代理（Caddy / Nginx）后面时，直连的对端永远是 127.0.0.1，
    X-Forwarded-For 的第一个才是客户端。因为应用建议只监听 127.0.0.1、
    由代理转进来，这个头是我们自己的代理写的，可以信任。
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def current_user(request: Request) -> dict:
    """取当前登录人；中间件已校验过，这里只是兜一层。"""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    return user


def require_admin(request: Request) -> dict:
    user = current_user(request)
    if user.get("role") != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="只有管理员可以做这个操作")
    return user


@router.get("/status")
def auth_status(request: Request) -> dict:
    """登录页/首次设置页的第一条请求 —— 必须是公开的。"""
    token = request.cookies.get(SESSION_COOKIE, "")
    user = auth_service.resolve_session(token) if token else None
    return {
        "initialized": auth_service.is_initialized(),
        "user": user,
    }


@router.post("/setup")
def auth_setup(payload: SetupPayload, response: Response, request: Request) -> dict:
    """首次创建管理员；建完直接登录，不用再输一遍。"""
    try:
        user = auth_service.create_first_admin(
            payload.username, payload.password, payload.display_name
        )
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    token = auth_service.create_session(user["id"], request.headers.get("user-agent", ""))
    _set_session_cookie(response, token)
    return {"ok": True, "user": user}


@router.post("/login")
def auth_login(payload: LoginPayload, response: Response, request: Request) -> dict:
    ip = _client_ip(request)
    blocked = auth_service.login_blocked_reason(ip, payload.username)
    if blocked:
        raise HTTPException(status_code=429, detail=blocked)
    try:
        user = auth_service.authenticate(payload.username, payload.password)
    except AuthError as exc:
        auth_service.register_login_failure(ip, payload.username)
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    auth_service.clear_login_failures(ip, payload.username)
    token = auth_service.create_session(user["id"], request.headers.get("user-agent", ""))
    _set_session_cookie(response, token)
    return {"ok": True, "user": user}


@router.post("/logout")
def auth_logout(request: Request, response: Response) -> dict:
    auth_service.delete_session(request.cookies.get(SESSION_COOKIE, ""))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def auth_me(request: Request) -> dict:
    return {"user": current_user(request)}


@router.patch("/profile")
def auth_update_profile(payload: ProfilePayload, request: Request) -> dict:
    """改自己的显示名称 / 头像。角色和启用状态不在这里改（防提权）。"""
    user = current_user(request)
    try:
        updated = auth_service.update_own_profile(
            int(user["id"]),
            display_name=payload.display_name,
            avatar=payload.avatar,
        )
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "user": updated}


@router.post("/password")
def auth_change_password(payload: PasswordPayload, request: Request) -> dict:
    """改自己的密码。先验旧密码，改完当前设备要重新登录。"""
    user = current_user(request)
    try:
        auth_service.authenticate(user["username"], payload.old_password)
        auth_service.set_password(int(user["id"]), payload.new_password)
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "detail": "密码已修改，请用新密码重新登录"}


# ---------------- 界面偏好（跟着账号走）----------------

@router.get("/prefs")
def auth_get_prefs(request: Request) -> dict:
    """读当前账号的界面偏好。

    目前用于首页 Notifications Center 的区块排序：
        { "nc": { "sources": [...], "kinds": { "dingtalk": [...], ... } } }
    后端不解释内容，原样返回；没设置过就是空对象。
    """
    user = current_user(request)
    try:
        prefs = auth_service.get_ui_prefs(int(user["id"]))
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "prefs": prefs}


@router.patch("/prefs")
def auth_save_prefs(payload: dict, request: Request) -> dict:
    """整体覆盖保存界面偏好 —— 前端每次提交完整的一份。"""
    user = current_user(request)
    try:
        prefs = auth_service.save_ui_prefs(int(user["id"]), payload)
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "prefs": prefs}


# ---------------- 管理员：账号管理 ----------------

@router.get("/users")
def auth_list_users(request: Request) -> dict:
    require_admin(request)
    return {"users": auth_service.list_users()}


@router.post("/users")
def auth_create_user(payload: UserCreatePayload, request: Request) -> dict:
    require_admin(request)
    try:
        user = auth_service.create_user(
            payload.username,
            payload.password,
            payload.display_name,
            payload.role,
            payload.avatar,
        )
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "user": user}


@router.patch("/users/{user_id}")
def auth_update_user(user_id: int, payload: UserUpdatePayload, request: Request) -> dict:
    require_admin(request)
    try:
        user = auth_service.update_user(
            user_id,
            display_name=payload.display_name,
            role=payload.role,
            active=payload.active,
            avatar=payload.avatar,
        )
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "user": user}


class ResetPasswordPayload(BaseModel):
    new_password: str = ""


@router.post("/users/{user_id}/password")
def auth_reset_user_password(
    user_id: int, payload: ResetPasswordPayload, request: Request
) -> dict:
    """管理员重置别人的密码（不需要知道旧密码）。"""
    require_admin(request)
    try:
        auth_service.set_password(user_id, payload.new_password)
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "detail": "密码已重置，对方需要用新密码重新登录"}


@router.delete("/users/{user_id}")
def auth_delete_user(user_id: int, request: Request) -> dict:
    admin = require_admin(request)
    if int(admin["id"]) == int(user_id):
        raise HTTPException(status_code=400, detail="不能删除自己")
    try:
        auth_service.delete_user(user_id)
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}
