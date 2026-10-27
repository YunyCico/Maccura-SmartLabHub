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
        secure=False,           # 本机/内网是 http；上 HTTPS 后应改 True
        path="/",
    )


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
    try:
        user = auth_service.authenticate(payload.username, payload.password)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
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
