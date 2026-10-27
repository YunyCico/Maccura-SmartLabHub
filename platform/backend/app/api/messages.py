from typing import Any, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.services import message_service
from app.services.dingtalk_messages import DingTalkAdapter

router = APIRouter(prefix="/messages", tags=["messages"])


class UserIdRequest(BaseModel):
    """手机号换 userid 的请求体。

    凭据走请求体而不是 query string，避免 Secret 落到访问日志里。
    传了就用传的，没传就回落到已保存的配置。
    """

    mobile: str = ""
    app_key: str = ""
    app_secret: str = ""
    corp_id: str = ""


@router.get("")
def list_messages(
    user_id: str = Query(default="", description="钉钉 userid；留空时后端用配置默认值"),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    """首页 Notifications Center 的数据源：三家合并后的归一化消息列表。"""
    return message_service.collect_messages(user_id=user_id, limit=limit)


@router.get("/sources")
def message_sources() -> dict:
    """各来源的配置状态，用于首页显示"已连接 / 未配置"角标。"""
    return {"sources": message_service.source_state()}


@router.post("/dingtalk/userid")
def dingtalk_userid(payload: UserIdRequest) -> dict:
    """用手机号换钉钉 userid。

    没有开发者权限时 CorpId 不可见，前端免登换不了 userid；
    这个接口走旧版通道取 token，只依赖 Client ID/Secret。

    请求体里的凭据优先于已保存的配置——用户在设置页刚填了 Secret
    还没点保存时，这里也能直接用上，不用先保存再查。
    """
    try:
        user_id = DingTalkAdapter().resolve_user_id(
            payload.mobile,
            app_key=payload.app_key,
            app_secret=payload.app_secret,
            corp_id=payload.corp_id,
        )
    except ValueError as exc:
        return {"ok": False, "detail": str(exc)}
    except Exception as exc:
        return {"ok": False, "detail": _friendly_dingtalk_error(str(exc))}
    return {"ok": True, "user_id": user_id}


# 钉钉报错里最常见的三类，给出可操作的下一步而不是把英文原话甩给用户
_DINGTALK_ERROR_HINTS = (
    (
        ("缺少权限", "权限不足", "无权限", "not have permission", "qyapi_get_member"),
        "应用缺少「根据手机号获取成员基本信息」权限。请到钉钉开发者后台"
        "应用详情 → 权限管理 → 搜索「根据手机号获取成员基本信息」并申请开通，"
        "审批通过后回到这里再试一次。",
    ),
    (
        ("40104", "无效的手机号"),
        "该手机号在企业里无效。请确认手机号与钉钉通讯录中的登记完全一致"
        "（含国家区号时不要加 +86）。",
    ),
    (
        ("60121", "未找到该用户"),
        "通讯录里查不到这个手机号。请确认本人已录入组织通讯录且在职。",
    ),
    (
        ("Missing required arguments",),
        "接口参数不对（这是程序问题，不是配置问题）。请联系维护者检查调用参数。",
    ),
)


def _friendly_dingtalk_error(raw: str) -> str:
    """把钉钉的英文/编码报错翻译成用户看得懂的下一步。"""
    text = raw or ""
    for needles, hint in _DINGTALK_ERROR_HINTS:
        if any(needle in text for needle in needles):
            return f"{text}｜{hint}"
    return text
