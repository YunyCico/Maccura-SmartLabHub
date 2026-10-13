from typing import Optional

import httpx
from fastapi import APIRouter, Query

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
def current_user(code: Optional[str] = Query(default=None)) -> dict:
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
