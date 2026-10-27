from typing import Optional

import httpx
from fastapi import APIRouter, Query, Request

from app.core.config import get_settings

router = APIRouter(tags=["user"])

_LOCAL_USER = {
    "user_id": "local-admin",
    "name": "本地管理员",
    "role": "department_admin",
    "data_scope": "all",
    "avatar": "",
}

_OAPI = "https://oapi.dingtalk.com"


def _dingtalk_configured() -> bool:
    settings = get_settings()
    return bool(settings.dingtalk_app_key and settings.dingtalk_app_secret)


def _fetch_dingtalk_user(code: str) -> Optional[dict]:
    settings = get_settings()
    with httpx.Client(timeout=10) as client:
        token_resp = client.get(
            f"{_OAPI}/gettoken",
            params={"appkey": settings.dingtalk_app_key, "appsecret": settings.dingtalk_app_secret},
        ).json()
        if token_resp.get("errcode") != 0:
            return None
        access_token = token_resp["access_token"]

        info = client.post(
            f"{_OAPI}/topapi/v2/user/getuserinfo",
            params={"access_token": access_token},
            json={"code": code},
        ).json()
        if info.get("errcode") != 0:
            return None
        userid = info["result"]["userid"]

        detail = client.post(
            f"{_OAPI}/topapi/v2/user/get",
            params={"access_token": access_token},
            json={"userid": userid},
        ).json()
        if detail.get("errcode") != 0:
            return None
        result = detail["result"]

    return {
        "user_id": userid,
        "name": result.get("name") or _LOCAL_USER["name"],
        "role": _LOCAL_USER["role"],
        "data_scope": _LOCAL_USER["data_scope"],
        "avatar": result.get("avatar") or "",
    }


@router.get("/me")
def current_user(request: Request, code: Optional[str] = Query(default=None)) -> dict:
    """当前登录人。

    加了平台账号体系后，身份来源就是登录会话 —— 中间件已经校验过。
    钉钉 code 那条路径保留（拿它换 userid 给钉钉接口用），但不再决定"我是谁"。
    """
    session_user = getattr(request.state, "user", None)
    if session_user:
        return {
            "user_id": f"local-{session_user['id']}",
            "name": session_user.get("display_name") or session_user.get("username", ""),
            "role": session_user.get("role", _LOCAL_USER["role"]),
            "data_scope": _LOCAL_USER["data_scope"] if session_user.get("role") == "admin" else "own",
            "avatar": "",
            "username": session_user.get("username", ""),
        }
    # 理论上到不了这里（中间件会先返回 401），保留兜底
    if code and _dingtalk_configured():
        try:
            user = _fetch_dingtalk_user(code)
            if user:
                return user
        except httpx.HTTPError:
            pass
    return dict(_LOCAL_USER)


@router.get("/dingtalk/config")
def dingtalk_config() -> dict:
    settings = get_settings()
    return {
        "corp_id": settings.dingtalk_corp_id,
        "enabled": _dingtalk_configured(),
    }
