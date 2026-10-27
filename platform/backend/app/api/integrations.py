"""消息互通配置接口。

供设置页读写三家凭据与路径：
- GET  /api/integrations                 读配置（密码脱敏）
- PUT  /api/integrations                 保存配置
- POST /api/integrations/test            连通性自检
- POST /api/integrations/salesforce/login 登录 Salesforce 并保存会话
"""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel

from app.services import integration_config
from app.services.message_models import SOURCE_DINGTALK, SOURCE_OA, SOURCE_SALESFORCE
from app.services.message_service import invalidate_snapshot
from app.services.salesforce_messages import clear_board_cache, _safe_returl

router = APIRouter(prefix="/integrations", tags=["integrations"])

class IntegrationPayload(BaseModel):
    dingtalk: dict[str, Any] = {}
    oa: dict[str, Any] = {}
    salesforce: dict[str, Any] = {}


class SalesforceLoginPayload(BaseModel):
    """登录请求。空值表示沿用已保存的配置（前端可以只改其中一个字段）。"""

    instance_url: str = ""
    username: str = ""
    password: str = ""
    totp_secret: str = ""
    auth_mode: str = ""
    api_version: str = ""
    remember: bool = False


class OaLoginPayload(BaseModel):
    """OA 登录请求。OA 只要账号密码，**不需要动态令牌**。"""

    base_url: str = ""
    username: str = ""
    password: str = ""
    login_path: str = ""
    todo_path: str = ""
    parser: str = ""
    xml_path: str = ""
    page_id: str = ""
    remember: bool = True


@router.post("/oa/login")
def oa_login(payload: OaLoginPayload) -> dict:
    """登录 OA 并保存账号，之后后端每次拉数据自动登录。

    和 Salesforce 的区别：OA 没有双因素，只要账号密码。
    这里**不存会话 cookie**，而是存账号密码 + auth_mode=password，
    由适配器每次取数时自动登录 —— cookie 会过期，账号密码不会。
    """
    from app.services.oa_messages import OaAdapter

    stored = integration_config.load_config()["oa"]
    merged = dict(stored)
    for key, value in payload.model_dump().items():
        if value:
            merged[key] = value
    # 前端回传的是脱敏空串，空值沿用已保存的凭据
    if not payload.password and stored.get("password"):
        merged["password"] = stored["password"]

    if not merged.get("base_url"):
        return {"ok": False, "detail": "还没填 OA 系统地址"}
    if not merged.get("username") or not merged.get("password"):
        return {"ok": False, "detail": "还缺：登录账号 / 密码"}

    adapter = OaAdapter()
    adapter._settings = {**merged, "auth_mode": "password"}
    cookie, error = adapter.login_and_get_cookie()
    if error:
        return {"ok": False, "detail": error}

    # 登录成功 → 落盘账号密码，切成 password 模式（下次自动登录）
    updates = {
        "oa": {
            "base_url": merged.get("base_url", ""),
            "auth_mode": "password",
            "username": merged.get("username", ""),
            "password": merged.get("password", ""),
            "login_path": merged.get("login_path", ""),
            "todo_path": merged.get("todo_path", ""),
            "parser": merged.get("parser", ""),
            "xml_path": merged.get("xml_path", ""),
            "page_id": merged.get("page_id", ""),
            # 会话也存一份：账号密码万一失效，短时间内还能用这个 cookie 顶着
            "token": cookie,
        }
    }
    try:
        config = integration_config.save_config(updates)
    except OSError as exc:
        return {"ok": False, "detail": f"登录成功但保存失败：{exc}"}

    # 顺便真拉一次，确认能出数据（登录成功不等于能取到待办）
    count = 0
    probe_error = ""
    try:
        verify = OaAdapter()
        items = verify.fetch()
        count = len(items)
        probe_error = verify.error
    except Exception as exc:  # 探活失败不影响登录结果
        probe_error = str(exc)

    detail = f"登录成功，已取到 {count} 条待办"
    if not count and probe_error:
        detail = f"登录成功，但取数失败：{probe_error}"
    return {
        "ok": True,
        "detail": detail,
        "cookie_hint": integration_config.secret_hint(cookie),
        "count": count,
        "config": integration_config.public_view(config),
        "sources": _source_flags(config),
    }


@router.post("/salesforce/login")
def salesforce_login(payload: SalesforceLoginPayload) -> dict:
    """登录 Salesforce 并把会话 id 存进配置。

    走「密码 → 动态口令 → 会话」完整流程（适配已开双因素认证的实例）。
    登录成功后会话写回 integrations.json，之后首页直接用它拉数据；
    会话过期再点一次即可，不用手填。
    """
    from app.services.salesforce_messages import SalesforceAdapter

    stored = integration_config.load_config()["salesforce"]
    merged = dict(stored)
    for key, value in payload.model_dump().items():
        if value:
            merged[key] = value
    # 密码类字段留空表示"沿用已存的"，但后端脱敏后回传的是空串
    if not payload.password and stored.get("password"):
        merged["password"] = stored["password"]
    if not payload.totp_secret and stored.get("totp_secret"):
        merged["totp_secret"] = stored["totp_secret"]

    adapter = SalesforceAdapter()
    adapter._settings = merged

    # 「登录并获取 Token」的语义是"重新登录"，所以只要账号/密码/动态口令密钥齐全，
    # 就强制走一遍完整登录流程拿新会话——不管当前 auth_mode 是什么。
    # 否则（session 模式且没有动态口令密钥）退化为"验证现有会话是否还有效"。
    has_totp = bool(
        merged.get("username") and merged.get("password") and merged.get("totp_secret")
    )
    if has_totp:
        adapter._settings = {**merged, "auth_mode": "totp"}
        try:
            sid = adapter.login()
        except Exception as exc:
            return {"ok": False, "detail": f"登录失败：{exc}"}
    elif merged.get("access_token"):
        # 手上已有会话 id（任何鉴权模式）：先验证它还能不能用。
        # 注意 adapter.fetch() 内部会把异常吞掉转成 self._error，
        # 所以这里必须查 _error，否则会话已失效也会被误报成"仍可用"。
        try:
            items = adapter.fetch()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": f"现有会话已失效：{exc}"}
        if adapter._error:
            return {"ok": False, "detail": f"现有会话已失效：{adapter._error}"}
        sid = str(merged["access_token"])
        return {
            "ok": True,
            "detail": f"现有会话仍可用（已取到 {len(items)} 条）。"
                      "填上动态口令密钥后可点此按钮重新登录换取新会话。",
            "token_hint": integration_config.secret_hint(sid),
            "config": integration_config.public_view(integration_config.load_config()),
            "sources": _source_flags(integration_config.load_config()),
        }
    else:
        return {"ok": False, "detail": adapter._unconfigured_reason() or "凭据未配齐"}

    # 落盘策略：
    # - sid（access_token）永远保存（即"抓 sid"，首页三板块靠它拉数据）。
    # - 腾讯令牌（6 位动态码）一次性、30 秒刷新，绝不落盘——不写 totp_secret。
    # - 密码：仅当用户勾选「记住账号密码」(remember=true) 才保存；
    #   没勾选则清空，后端也不落盘。代价：会话过期后无法自动重登，
    #   必须重新填 账号 + 密码 + 6 位码。
    # - 用户名：保留。它不是机密（设置页本就明文回显），而且「要批准的项目」
    #   「销售周计划」要靠它反查当前用户 Id（该 org 的 /User/me 返回 404），
    #   一并清掉会让这两个板块永久变空。
    remember = bool(payload.remember)
    updates = {
        "salesforce": {
            "auth_mode": merged.get("auth_mode") or "totp",
            "instance_url": merged.get("instance_url", ""),
            "api_version": merged.get("api_version", ""),
            "username": merged.get("username", ""),
            "password": merged.get("password", "") if remember else "",
            "access_token": sid,
        }
    }
    # 未勾选「记住账号密码」：账号密码后端也不落盘（password 是 SECRET_FIELDS，
    # 空字符串会被 save_config 视为"不修改"，必须走显式 clear_secrets 才能真正抹掉）。
    clear_secrets = {"salesforce": ["password"]} if not remember else None
    try:
        config = integration_config.save_config(updates, clear_secrets=clear_secrets)
        invalidate_snapshot()
        clear_board_cache()
    except OSError as exc:
        return {"ok": False, "detail": f"登录成功但保存会话失败：{exc}"}
    return {
        "ok": True,
        "detail": f"登录成功，会话已保存（{integration_config.secret_hint(sid)}）",
        "token_hint": integration_config.secret_hint(sid),
        "config": integration_config.public_view(config),
        "sources": _source_flags(config),
    }


class SalesforceLaunchPayload(BaseModel):
    """申请浏览器免登跳转令牌。"""

    path: str = ""


def _launch_error_page(title: str, detail: str, back: str = "/") -> HTMLResponse:
    """跳转失败时给用户看的一页，而不是一段 JSON。

    跳转是在**新标签页**里发生的，失败信息如果只回 JSON，
    用户只会看到一屏白字，完全不知道出了什么事。
    """
    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
 body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
      background:#f5f6f8;font:14px/1.7 "Microsoft YaHei",-apple-system,sans-serif;color:#2c2c2a}}
 .box{{max-width:560px;background:#fff;border:1px solid #e5e5ea;border-radius:14px;padding:32px 34px;
       box-shadow:0 2px 10px rgba(0,0,0,.05)}}
 h1{{margin:0 0 12px;font-size:18px;font-weight:500}}
 p{{margin:8px 0;color:#55555c}}
 code{{background:#f2f2f5;border-radius:5px;padding:2px 6px;font-size:12.5px;word-break:break-all}}
 a{{display:inline-block;margin-top:18px;background:#185fa5;color:#fff;text-decoration:none;
   padding:9px 20px;border-radius:999px;font-size:13px}}
</style></head>
<body><div class="box">
<h1>{title}</h1>
<p>{detail}</p>
<p><code>{back}</code></p>
<a href="{back}">← 返回工作平台</a>
</div></body></html>"""
    return HTMLResponse(html, status_code=502)


def _launch_is_same_site(request: Request) -> bool:
    """判断这次跳转是不是从平台自身发起的。

    浏览器会自动带 `Sec-Fetch-Site`：平台页点按钮跳同源地址是 `same-origin`，
    外部网站发起的则是 `cross-site`。这里只挡 cross-site，
    缺这个头的老浏览器一律放行（宁可宽松，也不要误伤正常点击）。
    """
    site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    if not site:
        return True
    return site in ("same-origin", "same-site", "none")


@router.post("/salesforce/launch")
def salesforce_launch(payload: SalesforceLaunchPayload) -> dict:
    """申请一次性免登令牌：让浏览器不重复登录就能进 Salesforce 原生页。

    为什么需要：后端自己登录拿到的 sid 只在服务器上，浏览器里没有会话，
    首页点「查看全部」直接开原生页会被 Salesforce 踢回登录页。
    这里用 sid 换 Salesforce 官方的 frontdoor URL，浏览器访问后会自动认领会话。

    **不把 sid 交给前端**（会进浏览器历史和访问日志），
    前端只拿一个一次性、60 秒过期的令牌。
    """
    from app.services.salesforce_messages import issue_launch_token

    try:
        token, ttl = issue_launch_token(payload.path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "token": token, "expires_in": ttl, "path": payload.path}


@router.get("/salesforce/launch")
def salesforce_launch_redirect(
    request: Request,
    path: str = Query(default=""),
    t: str = Query(default=""),
) -> Response:
    """免登跳转：302 到 Salesforce 的 frontdoor。

    两种入参：
    - `?path=/007/o`：**前端直接导航用**。一次同步跳转，不绕前端、不依赖异步，
      失败时直接在这一页给出可读的错误说明（不会留下空白标签页）。
    - `?t=<token>`：消费 POST 领到的一次性令牌（给 API 客户端用）。
    """
    from app.services.salesforce_messages import (
        SalesforceAdapter,
        consume_launch_token,
    )

    if not _launch_is_same_site(request):
        return _launch_error_page(
            "请求来源不被允许",
            "这个跳转只能从工作平台自身发起。请回到工作平台首页再点一次。",
            "/",
        )

    if t:
        try:
            returl = consume_launch_token(t)
        except ValueError as exc:
            return _launch_error_page("跳转令牌已失效", str(exc), "/")
    else:
        try:
            returl = _safe_returl(path)
        except ValueError as exc:
            return _launch_error_page("跳转路径不合法", str(exc), "/")

    adapter = SalesforceAdapter()
    try:
        target = adapter.launch_redirect(returl)
    except Exception as exc:  # noqa: BLE001
        return _launch_error_page(
            "打不开 Salesforce",
            f"{exc}<br>如果刚改过平台代码，请运行「停止平台.bat」再运行「启动平台.bat」，"
            "让后端加载新代码。",
            "/",
        )

    response = RedirectResponse(target, status_code=302)
    # sid 会出现在这一次跳转的 URL 里，别让它顺着 Referer 漏给第三方
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


def _sf_section(fetcher, cache_key: str = "") -> dict:
    """取 Salesforce 首页某板块：复用已存 sid；失败回传错误供前端提示重登。

    不强制要求 totp_secret（登录后 sid 已落盘），会话过期才需要回前端重新登录。

    带共享缓存：首页会定时轮询，而这几个接口每次都要真打 Salesforce
    （实测单次 0.4~1.3 秒）。任一板块发现缓存过期时，服务层会把**四个板块一起取好**，
    所以一轮轮询只打 1 次上游，而不是 4 次。
    """
    from app.services.salesforce_messages import fetch_boards_cached

    if cache_key:
        boards = fetch_boards_cached()
        hit = boards.get(cache_key)
        if hit is not None:
            return hit

    return _sf_section_uncached(fetcher)


def _sf_section_uncached(fetcher) -> dict:
    """真去取一个板块（不走缓存）。"""
    from app.services.salesforce_messages import SalesforceAdapter

    cfg = integration_config.load_config()["salesforce"]
    instance_url = str(cfg.get("instance_url") or "").strip()
    result: dict[str, Any] = {"instance_url": instance_url, "connected": False, "error": "", "items": []}
    if not instance_url:
        result["error"] = "尚未配置 Salesforce 实例地址"
        return result
    adapter = SalesforceAdapter()
    try:
        result["items"] = fetcher(adapter)
        result["connected"] = True
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
    return result


@router.get("/salesforce/today-tasks")
def salesforce_today_tasks() -> dict:
    """首页「今天的任务」：我名下今天到期 + 逾期未完成的 Task（与原生首页口径一致）。"""
    return _sf_section(lambda a: a.fetch_today_tasks(), "today_tasks")


@router.get("/salesforce/my-events")
def salesforce_my_events() -> dict:
    """首页「今日日程」：我名下今天及以后结束的 Event（对应原生首页「今日事件」）。"""
    return _sf_section(lambda a: a.fetch_my_events(), "my_events")


@router.get("/salesforce/approvals")
def salesforce_approvals() -> dict:
    """首页「要批准的项目」：指派给当前登录人的待审批工作项。"""
    return _sf_section(lambda a: a.fetch_my_approvals(), "approvals")


@router.get("/salesforce/weekly-plans")
def salesforce_weekly_plans() -> dict:
    """首页「销售周计划」：当前登录人名下（OwnerId）的周计划。"""
    return _sf_section(lambda a: a.fetch_my_weekly_plans(), "weekly_plans")


@router.get("/dingtalk/collector")
def dingtalk_collector_status() -> dict:
    """钉钉事件推送采集器的状态。

    采集器用 Stream 模式（WebSocket 长连接，不需要公网地址）接收
    「日程变更」事件，把每条日程的创建人记到本地库 —— 这是钉钉开放接口
    唯一不返回、却能拿到创建人的途径。
    """
    from app.core.database import count_event_creators
    from app.services.dingtalk_stream import status as stream_status

    return {"ok": True, "collector": stream_status(), "recorded_total": count_event_creators()}


@router.get("/dingtalk/reminders")
def dingtalk_reminders(
    user_id: str = Query(default="", description="钉钉 userid；留空用配置里的默认值"),
    limit: int = Query(default=20, ge=1, le=50),
) -> dict:
    """首页「消息提醒」：日程提醒 + 消息人（组织者）与参与人，带头像。

    钉钉没有收件箱查询接口，能拿到"提醒"的只有日程的 reminders 字段。
    头像需要 Contact.User.Read 权限，未开通时 avatar_url 为空，
    前端用姓名首字兜底。
    """
    from app.services.dingtalk_messages import DingTalkAdapter

    adapter = DingTalkAdapter()
    if not adapter.configured():
        return {
            "connected": False,
            "error": "钉钉未配置凭据",
            "note": "",
            "items": [],
        }
    items = adapter.fetch_reminders(user_id=user_id, limit=limit)
    note = adapter._kind_notes.get("remind", "")
    error = "；".join(adapter._errors[:2])
    return {
        "connected": not error,
        "error": error,
        "note": note,
        "items": items,
    }


@router.get("/oa/todos")
def oa_todos() -> dict:
    """首页「OA 业务看板」的待办列表。"""
    from app.services.oa_messages import OaAdapter

    adapter = OaAdapter()
    if not adapter.configured():
        return {
            "instance_url": "",
            "connected": False,
            "error": adapter.unconfigured_reason(),
            "items": [],
        }
    items = adapter.fetch()
    return {
        "instance_url": str(adapter._settings.get("base_url") or ""),
        "connected": not adapter.error,
        "error": adapter.error,
        "items": [
            {
                "id": item.external_id or item.title,
                "title": item.title,
                "creator": item.creator,
                "due": item.due,
                "priority": item.priority,
                "url": item.url,
                "detail": item.detail,
            }
            for item in items
        ],
    }


@router.post("/oa/disconnect")
def oa_disconnect() -> dict:
    """断开公司 OA：清掉凭据与接口路径，恢复到"未配置"状态。

    OA 没有 Salesforce 那样的会话概念（凭据直接用于取数），
    所以断开就是把整段配置抹掉，回到需要重新填写的状态。
    """
    # 只清空"配置类"字段，保留 timeout_seconds / max_items 这类数值设置
    oa_fields = {
        key: ""
        for key in (
            "base_url", "auth_mode", "username", "password", "token",
            "auth_header", "login_path", "todo_path", "todo_query_params",
            "rows_path", "field_map", "default_title",
        )
    }
    try:
        config = integration_config.save_config(
            {"oa": oa_fields},
            clear_secrets={"oa": ["password", "token"]},
        )
        invalidate_snapshot()
        clear_board_cache()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"断开失败：{exc}") from exc
    return {
        "ok": True,
        "detail": "已断开 OA 连接",
        "config": integration_config.public_view(config),
        "sources": _source_flags(config),
    }


@router.get("")
def read_integrations() -> dict:
    config = integration_config.load_config()
    return {
        "config": integration_config.public_view(config),
        "sources": _source_flags(config),
        "storage": str(integration_config.DATA_FILE),
    }


@router.post("/salesforce/disconnect")
def salesforce_disconnect() -> dict:
    """断开 Salesforce：清掉本机保存的会话 id。

    首页消息中心的连接面板「断开连接」用这个接口。断开后
    configured() 变回 False，前端就会自动展开登录界面。

    账号密码属于"凭据"，是用户主动勾选「记住」才存的，断开连接不牵连它们
    （下次重登仍可复用）。若用户想连凭据一起清，去设置页把密码改掉即可。
    """
    try:
        config = integration_config.save_config(
            {"salesforce": {"access_token": "", "totp_secret": ""}},
            clear_secrets={"salesforce": ["access_token", "totp_secret"]},
        )
        invalidate_snapshot()
        clear_board_cache()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"断开失败：{exc}") from exc
    return {
        "ok": True,
        "detail": "已断开 Salesforce 连接",
        "config": integration_config.public_view(config),
        "sources": _source_flags(config),
    }


@router.put("")
def write_integrations(payload: IntegrationPayload) -> dict:
    updates = {
        "dingtalk": payload.dingtalk,
        "oa": payload.oa,
        "salesforce": payload.salesforce,
    }
    # 丢弃白名单外的键，避免任意写入
    for section, values in updates.items():
        allowed = integration_config.ALLOWED_FIELDS.get(section, [])
        updates[section] = {k: v for k, v in values.items() if k in allowed}

    # 腾讯身份验证器的 6 位动态码是 30 秒刷新的一次性凭证，绝不能落盘当长期密钥用
    # （存下来也会立刻失效，只会让下次自动重登莫名失败）。此处直接丢弃。
    sf_totp = str(updates["salesforce"].get("totp_secret") or "").strip()
    if re.fullmatch(r"\d{6}", sf_totp):
        updates["salesforce"].pop("totp_secret", None)

    try:
        config = integration_config.save_config(updates)
        invalidate_snapshot()
        clear_board_cache()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"配置保存失败：{exc}") from exc
    return {
        "ok": True,
        "config": integration_config.public_view(config),
        "sources": _source_flags(config),
    }


@router.post("/test")
def test_connection(payload: IntegrationPayload) -> dict:
    """按提交上来的配置做一次连通性检查，不落盘。"""
    return {
        "dingtalk": _test_dingtalk(payload.dingtalk),
        "oa": _test_oa(payload.oa),
        "salesforce": _test_salesforce(payload.salesforce),
    }


def _test_salesforce(values: dict[str, Any]) -> dict[str, str]:
    """Salesforce 连通性自检：用提交上来的凭据真跑一次取数。"""
    from app.services.salesforce_messages import SalesforceAdapter

    stored = integration_config.load_config()["salesforce"]
    merged = {**stored, **{k: v for k, v in (values or {}).items() if v != ""}}
    adapter = SalesforceAdapter()
    adapter._settings = merged
    if not adapter.configured():
        return {"ok": False, "detail": adapter._unconfigured_reason() or "凭据未配齐"}
    try:
        items = adapter.fetch()
    except Exception as exc:  # 自检不抛给前端，只回话
        return {"ok": False, "detail": f"取数失败：{exc}"}
    # fetch() 内部会把异常吞掉转存到 _error 并返回空列表，
    # 不检查的话会话已失效也会被报成"连接成功但没有待办"。
    if adapter._error:
        return {"ok": False, "detail": f"取数失败：{adapter._error}"}
    if not items:
        return {"ok": True, "detail": "连接成功，但这个账号当前没有待办或日程"}
    kinds = {}
    for item in items:
        kinds[item.category] = kinds.get(item.category, 0) + 1
    detail = "、".join(f"{name} {count} 条" for name, count in kinds.items())
    return {"ok": True, "detail": f"连接成功：{detail}"}


def _source_flags(config: dict[str, Any]) -> list[dict[str, Any]]:
    from app.services.salesforce_messages import salesforce_configured

    dingtalk = config.get("dingtalk", {})
    oa = config.get("oa", {})
    salesforce = config.get("salesforce", {})
    return [
        {
            "source": SOURCE_DINGTALK,
            "label": "钉钉",
            "enabled": bool(dingtalk.get("app_key") and dingtalk.get("app_secret")),
            "missing": _missing(
                {"app_key": "Client ID", "app_secret": "Client Secret"},
                dingtalk,
            ),
        },
        {
            "source": SOURCE_OA,
            "label": "公司 OA",
            "enabled": bool(oa.get("base_url") and oa.get("todo_path") and oa.get("field_map")),
            "missing": _missing(
                {"base_url": "系统地址", "todo_path": "待办接口路径", "field_map": "字段映射"},
                oa,
            ),
        },
        {
            "source": SOURCE_SALESFORCE,
            "label": "Salesforce",
            # 与适配器共用同一份判定，避免设置页角标与首页角标口径不一致
            "enabled": salesforce_configured(salesforce),
            "missing": _missing({"instance_url": "实例地址"}, salesforce),
        },
    ]


def _missing(required: dict[str, str], values: dict[str, Any]) -> list[str]:
    return [label for key, label in required.items() if not values.get(key)]


def _test_dingtalk(values: dict[str, Any]) -> dict[str, str]:
    stored = integration_config.load_config()["dingtalk"]
    app_key = values.get("app_key") or stored.get("app_key")
    app_secret = values.get("app_secret") or stored.get("app_secret")
    corp_id = (values.get("corp_id") or stored.get("corp_id") or "").strip()
    if not app_key or not app_secret:
        return {"ok": False, "detail": "缺少 Client ID / Client Secret"}

    import httpx

    def legacy() -> tuple[bool, str]:
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                "https://oapi.dingtalk.com/gettoken",
                params={"appkey": app_key, "appsecret": app_secret},
            ).json()
        if resp.get("errcode") != 0:
            return False, str(resp.get("errmsg") or "取 token 失败")
        return True, "凭据有效（旧版通道，无需 CorpId）"

    if not corp_id:
        try:
            ok, detail = legacy()
        except httpx.HTTPError as exc:
            return {"ok": False, "detail": f"请求失败：{exc}"}
        if ok:
            return {"ok": True, "detail": f"{detail}；后续可补 CorpId 走新版通道"}
        return {"ok": False, "detail": detail}

    # 有 CorpId 优先新版，失败再回落旧版
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                f"https://api.dingtalk.com/v1.0/oauth2/{corp_id}/token",
                json={
                    "client_id": app_key,
                    "client_secret": app_secret,
                    "grant_type": "client_credentials",
                },
            )
        data = resp.json()
        if data.get("access_token"):
            return {"ok": True, "detail": "凭据有效，已通过新版通道换取 access_token"}
        v1_detail = str(data.get("message") or data.get("code") or resp.status_code)
    except httpx.HTTPError as exc:
        v1_detail = f"请求失败：{exc}"

    try:
        ok, detail = legacy()
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": f"新版失败（{v1_detail}）；旧版也失败：{exc}"}
    if ok:
        return {"ok": True, "detail": f"CorpId 可能有误，已自动走旧版通道：{detail}"}
    return {"ok": False, "detail": f"新版：{v1_detail}；旧版：{detail}"}


def _test_oa(values: dict[str, Any]) -> dict[str, str]:
    base_url = (values.get("base_url") or "").strip().rstrip("/")
    todo_path = (values.get("todo_path") or "").strip()
    if not base_url:
        return {"ok": False, "detail": "请先填写 OA 系统地址"}
    if not todo_path:
        return {"ok": False, "detail": "请先填写待办接口路径"}

    import httpx

    url = f"{base_url}/{todo_path.lstrip('/')}"
    headers = {"Accept": "application/json"}
    if values.get("auth_header") and values.get("token"):
        headers[values["auth_header"]] = values["token"]
    elif values.get("token"):
        headers["Cookie"] = values["token"]
    try:
        with httpx.Client(follow_redirects=True, timeout=10) as client:
            resp = client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": f"请求失败：{exc}"}

    if resp.status_code >= 400:
        return {"ok": False, "detail": f"HTTP {resp.status_code}"}
    # 泛微未登录时统一返回这个结构，据此判断是否鉴权失败
    if '"errorCode":"001"' in resp.text.replace(" ", "") or '"status":false' in resp.text:
        return {"ok": False, "detail": "接口可达但未通过鉴权，请检查账号密码或 token"}
    return {"ok": True, "detail": f"接口可达（HTTP {resp.status_code}）"}