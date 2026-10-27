"""Salesforce 适配器。

## 实例信息（2026-10-21 实地探测确认）

- Lightning 界面：`https://maccuracn.lightning.sfcrmapps.cn`
- My Domain（**API 主机**，登录表单 action 指向这里）：
  `https://maccuracn.my.sfcrmproducts.cn`
- orgId：`00DC8000009rygr`
- 可用 API 版本：31.0 ~ 67.0（默认取 62.0）
- OAuth token 端点：`POST {mydomain}/services/oauth2/token`
  （lightning 主机上是 302 跳转，**必须用 My Domain**）

## 鉴权方式

账号 `myun@maccura.com` **启用了 TOTP 双因素认证**（Authenticator App），
实测流程：

1. `POST {mydomain}/` 提交 un/pw → 302 → `/secur/frontdoor.jsp?sid=...`
2. 跳到 `/_ui/identity/verification/method/TotpVerificationUi/e`，
   页面里是待验证表单（`_CONFIRMATIONTOKEN` / `vcsrf` / `tc` / `save` 等）
3. 填入 6 位动态口令提交 → 会话才真正生效

**未完成 TOTP 的 sid 查不了数据**：
`GET /services/data/v62.0/query` 会返回
`{"errorCode":"INVALID_SESSION_ID"}`（HTTP 401）。

所以支持两种鉴权模式：

- **`totp`（默认，本实例用这个）**：用户名 + 密码 + TOTP 密钥，
  后端自己走完「密码 → TOTP → sid」全流程，sid 失效时自动重登。
  TOTP 密钥是 RFC 6238 的 base32 密钥（Authenticator App 设置页的
  "手动输入密钥 / manual entry key"），**不是** 那串 6 位验证码。
- **`oauth`**：`client_id` + `client_secret`（Connected App）
  或 `refresh_token`，走标准 OAuth，换 access_token 后调 REST。
  这条路适合以后由管理员建 Connected App 后切换，MFA 免疫。

TOTP 用 RFC 6238 自己实现（十几行），不引第三方依赖。
"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import logging
import re
import secrets
import struct
import threading
import time
from datetime import datetime, timedelta, timezone
from html import unescape as _html_unescape
from typing import Any
from urllib.parse import quote

import httpx

from app.services import integration_config
from app.services.message_models import (
    PRIORITY_MEDIUM,
    SOURCE_SALESFORCE,
    MessageItem,
    format_due,
    normalize_priority,
)

logger = logging.getLogger(__name__)

# 进程内会话缓存：key = (实例地址, 用户名)，value = (sid, 过期时间戳)
# 适配器每次 fetch 都新建，不缓存的话首页每刷新一次就要重新走一遍登录流程。
_SESSION_CACHE: dict[tuple[str, str], tuple[str, float]] = {}
# 会话保守按 50 分钟算（Salesforce 默认 2 小时无操作过期）
_SESSION_TTL = 50 * 60
# 对象业务名缓存：key = SObject 名，value = label（如 Feedback__c → 客户反馈/回访）。
# 取一次 describe 就够，缓存住避免每张审批卡片都去问一遍。
_OBJECT_LABEL_CACHE: dict[str, str] = {}

# ---- 浏览器免登跳转（frontdoor）的一次性令牌 ----
#
# 背景：后端自己登录拿到 sid（只存在服务器内存/配置里），浏览器那边没有会话，
# 所以首页点「查看全部」直接 window.open(Salesforce 原生页) 会被踢回登录页。
# 解法：后端用 sid 换 Salesforce 官方的 frontdoor 免登 URL，让浏览器去认领会话。
#
# 为什么不把 sid 直接交给前端：sid 进了 URL 就会进浏览器历史、Referer 和
# 访问日志。所以前端只拿到一个**一次性、60 秒过期**的令牌，
# 由跳转端点消费后换成真正的 frontdoor URL（302）。
_LAUNCH_TOKENS: dict[str, tuple[float, str]] = {}
_LAUNCH_TOKEN_TTL = 60.0
_LAUNCH_TOKEN_MAX = 64
_LAUNCH_LOCK = threading.Lock()

# 首页板块快照缓存：key = 板块名，value = (取数时刻, 数据)。
#
# 为什么需要：首页会定时轮询，而每个板块都要真打 Salesforce
# （实测单次 0.4~1.3 秒）。四个板块若各自独立缓存，一轮轮询就是 4~5 次
# 上游调用，API 配额很快见底。改成"任一板块发现过期就把四个一起取好"，
# 一轮只打 1 次上游。
#
# 这层缓存放在服务层，是为了让 API 的板块接口与消息聚合的探活共用同一份结果，
# 避免同一轮里重复打 Salesforce。
_BOARD_TTL = 20.0
_BOARD_LOCK = threading.Lock()
_BOARD_CACHE: dict[str, tuple[float, Any]] = {}


def _board_cache_get(key: str) -> Any | None:
    now = time.monotonic()
    with _BOARD_LOCK:
        hit = _BOARD_CACHE.get(key)
        if hit and (now - hit[0]) < _BOARD_TTL:
            return hit[1]
    return None


def _board_cache_put(key: str, data: Any) -> None:
    with _BOARD_LOCK:
        _BOARD_CACHE[key] = (time.monotonic(), data)


def clear_board_cache() -> None:
    """清掉板块快照。配置变更（登录/断开/保存）后调用。"""
    with _BOARD_LOCK:
        _BOARD_CACHE.clear()


def _cache_key(settings: dict[str, Any]) -> tuple[str, str]:
    return (
        str(settings.get("instance_url") or "").rstrip("/"),
        str(settings.get("username") or ""),
    )


def _safe_returl(path: str) -> str:
    """只放行**站内相对路径**，挡住开放重定向。

    跳转目标最终会拼进 Salesforce 的 frontdoor URL，如果这里允许绝对地址或
    `//evil.com` 这种协议相对地址，用户点一下就可能被带到外站。
    """
    value = str(path or "").strip()
    if not value.startswith("/"):
        raise ValueError("跳转路径必须是站内相对路径（以 / 开头）")
    if value.startswith("//") or "://" in value:
        raise ValueError("跳转路径不允许包含协议或跨域前缀")
    if "\\" in value or ".." in value:
        raise ValueError("跳转路径不合法")
    return value


def issue_launch_token(path: str) -> tuple[str, float]:
    """签发一次性免登跳转令牌，返回 (token, 有效期秒数)。"""
    returl = _safe_returl(path)
    token = secrets.token_urlsafe(24)
    now = time.monotonic()
    with _LAUNCH_LOCK:
        for key in [k for k, (exp, _) in _LAUNCH_TOKENS.items() if exp <= now]:
            _LAUNCH_TOKENS.pop(key, None)
        while len(_LAUNCH_TOKENS) >= _LAUNCH_TOKEN_MAX:
            _LAUNCH_TOKENS.pop(next(iter(_LAUNCH_TOKENS)))
        _LAUNCH_TOKENS[token] = (now + _LAUNCH_TOKEN_TTL, returl)
    return token, _LAUNCH_TOKEN_TTL


def consume_launch_token(token: str) -> str:
    """消费一次性令牌换取 retURL —— **用完即焚**，同一个令牌不能复用。"""
    with _LAUNCH_LOCK:
        entry = _LAUNCH_TOKENS.pop(str(token or ""), None)
    if not entry:
        raise ValueError("跳转令牌无效或已被使用")
    expires, returl = entry
    if expires <= time.monotonic():
        raise ValueError("跳转令牌已过期，请重新点击")
    return returl


DEFAULT_MAX_ITEMS = 30
DEFAULT_TIMEOUT = 20
DEFAULT_API_VERSION = "v62.0"
# 回溯天数：实测这个 org 的未关闭 Task 挂了很久（ActivityDate 2026-08-01），
# 30 天窗口会把它们全滤掉，所以默认放宽到 90 天。
DEFAULT_WINDOW_DAYS = 90


def salesforce_configured(settings: dict[str, Any]) -> bool:
    """能否真正取到数——适配器与设置页共用这一份判定，避免两处口径不一致。

    totp 模式下「已存会话 id（access_token）」本身就算已配置：
    腾讯身份验证器的 6 位动态码按设计绝不落盘，所以 totp_secret 常态为空；
    若把 totp_secret 当成必要条件，登录成功后反而会被误判成"未配置"
    （首页显示示例数据、角标变灰、设置页自检直接报"还缺：TOTP 密钥"）。
    """
    s = settings or {}
    if not s.get("instance_url"):
        return False
    mode = str(s.get("auth_mode") or "totp").strip()
    if mode == "oauth":
        return bool(
            s.get("access_token")
            or (s.get("client_id") and (s.get("client_secret") or s.get("refresh_token")))
        )
    if mode == "session":
        return bool(s.get("access_token"))
    # totp：已有会话可直接复用；否则要 账号+密码+TOTP 密钥 才能自己重新登录
    return bool(s.get("access_token")) or bool(
        s.get("username") and s.get("password") and s.get("totp_secret")
    )


# Salesforce Task.Status 的常用取值 → 中文
_TASK_STATUS = {
    # 实测这个 org 的未关闭 Task 的 Status 就是 "Open"（不是 "Not Started"）
    "open": "未开始",
    "not started": "未开始",
    "in progress": "进行中",
    "completed": "已完成",
    "waiting on someone else": "等待他人",
    "deferred": "已延期",
    "closed": "已关闭",
}

# 默认 SOQL：未关闭的待办，最近到期在前
DEFAULT_TASK_SOQL = (
    "SELECT Id, Subject, ActivityDate, Status, Priority, Description, "
    "CreatedDate, Who.Name, What.Name, Owner.Name FROM Task "
    "WHERE IsClosed = false AND OwnerId = '{owner}' AND ActivityDate >= {min_date} "
    "ORDER BY ActivityDate ASC NULLS LAST LIMIT {limit}"
)

# 默认 SOQL：查询窗口内的日程
# 注意：StartDateTime 是 DateTime 类型，过滤值必须带时间（2026-09-21T00:00:00+08:00），
# 只给日期会报 "value of filter criterion for field 'StartDateTime' must be a date-time"
DEFAULT_EVENT_SOQL = (
    "SELECT Id, Subject, StartDateTime, EndDateTime, IsAllDayEvent, "
    "Location, Description, CreatedDate FROM Event "
    "WHERE OwnerId = '{owner}' AND StartDateTime >= {min_datetime} AND StartDateTime <= {max_datetime} "
    "ORDER BY StartDateTime ASC LIMIT {limit}"
)

_TOTP_PATH = "/_ui/identity/verification/method/TotpVerificationUi/e"


# ---------- TOTP（RFC 6238，自实现避免多引一个依赖） ----------


def totp_code(secret: str, moment: datetime | None = None, step: int = 30, digits: int = 6) -> str:
    """按 RFC 6238 生成 TOTP 动态口令。

    secret 是 base32 密钥（Authenticator App 里的"手动输入密钥"），
    不是那串 6 位验证码。
    """
    raw = (secret or "").strip().replace(" ", "").upper()
    if not raw:
        raise ValueError("缺少 TOTP 密钥")
    # base32 解码要补 = ，长度不是 8 的倍数时补齐
    padding = "=" * (-len(raw) % 8)
    try:
        key = base64.b32decode(raw + padding, casefold=True)
    except Exception as exc:  # 密钥格式不对时给个能看懂的提示
        raise ValueError(f"TOTP 密钥格式不对（应为 base32）：{exc}") from exc
    if not key:
        raise ValueError("TOTP 密钥解码后为空")
    counter = int((moment or datetime.now(timezone.utc)).timestamp()) // step
    digest = hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10**digits)).zfill(digits)


def _slash_date(value: str) -> str:
    """日期显示成 2026/10/13 这种斜杠格式（与用户习惯一致）。"""
    text = str(value or "").strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return f"{text[0:4]}/{text[5:7]}/{text[8:10]}"
    return text


def _to_local_iso(value: Any) -> str:
    """把 Salesforce 返回的 UTC 时间串换算成东八区 ISO 串。

    该 org 的 Event 结束时间都是本地 23:59（UTC 15:59），不换算的话前端会显示
    成 15:59，与 Salesforce 原生面板看到的 23:59 对不上。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone(timedelta(hours=8))).isoformat()


def _today_iso() -> str:
    """今天零点的 ISO 时间戳（东八区），用于 Event.EndDateTime 这类 DateTime 字段比较。

    实测该 org 的 Event 结束时间都落在本地 23:59（如 UTC 15:59 = +08:00 的 23:59），
    所以按本地时区算今天起点才对得上原生「今日事件」面板。
    """
    tz8 = timezone(timedelta(hours=8))
    now = datetime.now(tz8)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def _hospital_of(asset: str) -> str:
    """从资产串里取医院名。

    实测格式：`产品编码*名称*型号;属性*品牌型号*序列号**医院名_备注`
    取 `**` 之后、`_` 之前的部分。
    """
    if not asset:
        return ""
    tail = asset.split("**", 1)[1] if "**" in asset else ""
    if not tail:
        return ""
    name = tail.split("_", 1)[0].strip()
    return name[:40]


def _safe_name(obj: Any) -> str:
    """从 SOQL 关系查询结果里安全取 Name（关系为 null 时返回空串）。"""
    if not isinstance(obj, dict):
        return ""
    return str(obj.get("Name") or "").strip()


def _strip_tags(html: str) -> str:
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _form_fields(html: str) -> dict[str, str]:
    """抓表单里的 hidden / text 字段（value 为空的也保留 key）。"""
    fields: dict[str, str] = {}
    for match in re.finditer(r"<input[^>]*>", html):
        tag = match.group(0)
        name = re.search(r'name="([^"]*)"', tag)
        if not name:
            continue
        ftype = re.search(r'type="([^"]*)"', tag)
        if ftype and ftype.group(1) not in ("hidden", "text"):
            continue
        value = re.search(r'value="([^"]*)"', tag)
        fields[name.group(1)] = value.group(1) if value else ""
    return fields


class SalesforceAdapter:
    def __init__(self) -> None:
        # 读集成配置文件优先于 .env，设置页保存后立即生效
        self._settings = integration_config.effective_settings()["salesforce"]
        # trust_env=False：这台机的代理环境变量会让 httpx 访问 Salesforce 直接 502
        self._client = httpx.Client(
            follow_redirects=True,
            timeout=self._timeout(),
            trust_env=False,
            headers={"User-Agent": "SmartLabHub/1.0"},
        )
        self._authenticated = False
        self._error = ""
        self._current_user_id_cache = ""

    # ---------- 配置判定 ----------

    def configured(self) -> bool:
        return salesforce_configured(self._settings)

    # ---------- 基础工具 ----------

    def _timeout(self) -> int:
        return int(self._settings.get("timeout_seconds") or DEFAULT_TIMEOUT)

    def _max_items(self) -> int:
        return int(self._settings.get("max_items") or DEFAULT_MAX_ITEMS)

    def _base(self) -> str:
        return str(self._settings.get("instance_url") or "").rstrip("/")

    def _api_version(self) -> str:
        version = str(self._settings.get("api_version") or DEFAULT_API_VERSION).strip()
        return version if version.startswith("v") else f"v{version}"

    # ---------- 鉴权 ----------

    def _oauth_token(self) -> str:
        """OAuth：client_credentials 或 refresh_token 换 access_token。"""
        s = self._settings
        base = self._base()
        data: dict[str, str] = {}
        client_id = str(s.get("client_id") or "")
        client_secret = str(s.get("client_secret") or "")
        refresh = str(s.get("refresh_token") or "")
        if refresh:
            data = {
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": client_id,
                "client_secret": client_secret,
            }
        else:
            data = {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            }
        resp = self._client.post(
            f"{base}/services/oauth2/token",
            data=data,
            headers={"Accept": "application/json"},
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Salesforce 取 token 失败（{resp.status_code}）：{resp.text[:200]}"
            )
        payload = resp.json()
        token = str(payload.get("access_token") or "").strip()
        if not token:
            raise RuntimeError(f"Salesforce 返回里没有 access_token：{resp.text[:160]}")
        return token

    def _login_totp(self) -> None:
        """用户名密码 → TOTP 动态口令 → 拿到可用会话。"""
        s = self._settings
        base = self._base()
        username = str(s.get("username") or "")
        password = str(s.get("password") or "")

        resp = self._client.post(
            f"{base}/",
            data={
                "un": username,
                "pw": password,
                "username": username,
                "loginType": "",
                "useSecure": "true",
                "login": "",
                "display": "page",
                "width": "1200",
                "height": "800",
                "lt": "standard",
            },
            headers={"Referer": f"{base}/"},
        )
        html = resp.text
        if 'name="tc"' not in html:
            text = _strip_tags(html)[:200]
            if "验证您的身份" in text:
                raise RuntimeError("Salesforce 登录未通过双因素验证")
            raise RuntimeError(f"Salesforce 登录未进入验证码页，返回：{text}")

        # 表单值里带 HTML 实体（例如 retURL 里的 &amp;），浏览器提交前会自动反转义；
        # httpx 不会，必须手工反转义，否则参数对不上服务器。
        form = {k: _html_unescape(v) for k, v in _form_fields(html).items()}
        # 用户填的「令牌」是腾讯身份验证器（Tencent Authenticator）实时出的 6 位动态码，
        # 直接作为 TOTP 验证页的 tc 提交即可；若填的是 base32 密钥（高级/一次性提取），
        # 则本地算出 6 位码再提交。两者都走同一套「密码 → TOTP 验证 → 抓 sid」。
        raw_token = str(s.get("totp_secret") or "").strip()
        if re.fullmatch(r"\d{6}", raw_token):
            form["tc"] = raw_token
        else:
            try:
                form["tc"] = totp_code(raw_token)
            except ValueError as exc:
                raise RuntimeError(
                    f"动态口令无效：请填腾讯身份验证器的 6 位动态码，或有效的 base32 密钥（{exc}）"
                ) from exc
        form["save"] = " 验证 "
        # 关键修复：必须 POST 到「带 ?vcsrf=...&vpol=..&vflid=..&vfgrp=.. 查询串」的验证码页 URL。
        # 若只 POST 到裸 action 路径（/_ui/identity/verification/method/TotpVerificationUi/e），
        # 会丢掉这些路由参数，Salesforce 找不到本次验证尝试，直接判「验证尝试已超时」——
        # 即使填的 6 位码完全正确也永远失败。这是之前登录一直失败的根本原因。
        verify = self._client.post(
            str(resp.url),
            data=form,
            headers={"Referer": str(resp.url)},
        )
        page = _strip_tags(verify.text)
        if "验证您的身份" in page:
            if "超时" in page:
                raise RuntimeError("Salesforce 验证会话已超时，请重新点击登录再试")
            if "无效" in page or "到期" in page or "错误" in page:
                raise RuntimeError(
                    "Salesforce 动态口令无效或已过期，请用腾讯身份验证器「最新」的 6 位码重试，"
                    "读码后请尽快点登录（验证码 30 秒刷新）"
                )
            raise RuntimeError("Salesforce 动态口令校验未通过，请用最新的 6 位码重试")
        sid = self._client.cookies.get("sid") or ""
        if not sid:
            # 兜底：TOTP 通过后会经 /secur/frontdoor.jsp?sid=... 跳转，
            # cookie 域若没抓到，可从跳转链 URL 里直接提取 sid。
            for _r in [verify, *verify.history]:
                _m = re.search(r"[?&]sid=([^&]+)", str(_r.url))
                if _m:
                    sid = _m.group(1)
                    break
        if not sid:
            raise RuntimeError("Salesforce 登录后未拿到会话 sid")
        self._authenticated = True
        _SESSION_CACHE[_cache_key(self._settings)] = (sid, time.time() + _SESSION_TTL)
        logger.info("Salesforce 登录成功（账号 %s）", username)
        return sid

    def login(self) -> str:
        """走「密码 → 动态口令 → 会话」完整流程，返回会话 id。

        设置页的「登录并获取 Token」按钮调的就是这个。
        """
        mode = str(self._settings.get("auth_mode") or "totp").strip()
        if mode == "oauth":
            return self._oauth_token()
        if mode == "session":
            token = str(self._settings.get("access_token") or "").strip()
            if not token:
                raise RuntimeError("未配置会话 id（access_token）")
            return token
        return self._login_totp()

    def _ensure_session(self) -> None:
        """确保有可用会话；OAuth 模式走 Bearer 头，totp 模式走 sid cookie。"""
        mode = str(self._settings.get("auth_mode") or "totp").strip()
        if mode in ("oauth", "session"):
            return
        if self._authenticated and self._client.cookies.get("sid"):
            return
        # 命中进程内缓存就不用重新走一遍登录
        cached = _SESSION_CACHE.get(_cache_key(self._settings))
        if cached and cached[1] > time.time():
            self._client.cookies.set("sid", cached[0], domain=".sfcrmproducts.cn")
            self._authenticated = True
            return
        # 进程内没有会话，但配置里存着上次登录抓到的 access_token（sid），
        # 先用它顶上（重启后端后也能复用），查询若返回 401 再自动重新登录。
        stored_sid = str(self._settings.get("access_token") or "").strip()
        if stored_sid:
            self._client.cookies.set("sid", stored_sid, domain=".sfcrmproducts.cn")
            self._authenticated = True
            return
        self._login_totp()

    def _headers(self) -> dict[str, str]:
        mode = str(self._settings.get("auth_mode") or "totp").strip()
        headers = {"Accept": "application/json"}
        if mode == "oauth":
            headers["Authorization"] = f"Bearer {self._oauth_token()}"
        elif mode == "session":
            # 直接用现成的会话 id（临时/应急用，会话过期就要重填）
            headers["Authorization"] = f"Bearer {self._session_token()}"
        else:
            # totp 模式：登录拿到的 sid 必须放 Authorization 头，
            # **光带 sid cookie 调 REST 会返回 INVALID_SESSION_ID**（实测）
            headers["Authorization"] = f"Bearer {self._session_token()}"
        return headers

    def _session_token(self) -> str:
        """取会话 id：session 模式取配置，totp 模式取登录后的 cookie。"""
        mode = str(self._settings.get("auth_mode") or "totp").strip()
        if mode == "session":
            token = str(self._settings.get("access_token") or "").strip()
            if not token:
                raise RuntimeError("未配置会话 id（access_token）")
            return token
        token = self._client.cookies.get("sid") or ""
        if not token:
            cached = _SESSION_CACHE.get(_cache_key(self._settings))
            token = cached[0] if cached else ""
        if not token:
            raise RuntimeError("尚未取得会话 sid")
        return token

    def launch_redirect(self, path: str) -> str:
        """生成浏览器免登跳转到 Salesforce 原生页的 URL。

        走 Salesforce 官方的 frontdoor：浏览器访问
        `https://<mydomain>/secur/frontdoor.jsp?sid=...&retURL=...` 后，
        Salesforce 会把 sid 落成浏览器 cookie（sid / sid_Client / oid）
        并跳到目标页 —— 这样用户就不必再在浏览器里登一次。

        实测（2026-10-26）：`/007/o`、`/00T/e`、`/00U/e`、`/a0y/o`
        以及记录详情路径 `/00T<Id>` 全部能正确落地，且不落到登录页。
        """
        returl = _safe_returl(path)
        base = self._base()
        if not base:
            raise RuntimeError("尚未配置 Salesforce 实例地址（instance_url）")
        try:
            self._ensure_session()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"Salesforce 会话不可用：{exc}。请到「设置 → 集成」重新登录一次。"
            ) from exc
        sid = self._session_token()
        if not sid:
            raise RuntimeError("尚未取得会话 sid，请先在「设置 → 集成」登录 Salesforce")
        return (
            f"{base}/secur/frontdoor.jsp"
            f"?sid={quote(sid, safe='')}"
            f"&retURL={quote(returl, safe='')}"
        )

    # ---------- 连通性探测 ----------

    def check_connection(self) -> bool:
        """只验证"会话还能不能用"，**不拉任何业务数据**。

        用于首页消息中心里 Salesforce 那一栏：用户只要求"登录成功即提示连接成功"，
        没必要为此把任务和日程明细全查一遍。用 describe 元数据接口探活最省
        （实测 ~0.2s，失效 sid 会返回 401 INVALID_SESSION_ID，可据此判会话过期）。

        返回 True 表示会话有效；会话过期时把原因写进 self._error，交给 status() 透出。
        """
        self._error = ""
        if not self.configured():
            return False
        try:
            self._ensure_session()
            url = f"{self._base()}/services/data/{self._api_version()}/sobjects/Task/describe"
            resp = self._client.get(url, headers=self._headers())
            if resp.status_code == 401 or "INVALID_SESSION_ID" in resp.text:
                raise RuntimeError("Salesforce 会话已失效，请重新登录")
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"Salesforce 连通性检查失败（{resp.status_code}）：{resp.text[:120]}"
                )
            return True
        except Exception as exc:  # noqa: BLE001
            self._error = str(exc)
            return False

    # ---------- 拉取 ----------

    def fetch(self, user_id: str = "") -> list[MessageItem]:
        if not self.configured():
            return []
        self._error = ""
        try:
            return self._fetch_rest()
        except Exception as exc:  # 单家失败不影响别家，与钉钉/OA 同策略
            logger.warning("Salesforce 拉取失败：%s", exc)
            self._error = str(exc)
            return []

    def _fetch_rest(self) -> list[MessageItem]:
        now = datetime.now(timezone.utc).astimezone()
        window = int(self._settings.get("window_days") or DEFAULT_WINDOW_DAYS)
        min_date = (now - timedelta(days=window)).date().isoformat()
        max_items = self._max_items()
        items: list[MessageItem] = []

        # 只取当前登录人自己的记录。org 全量在这个 org 里全是别人名下的
        # 集成失败日志（128 条同 Subject 的 Apex 异常任务），当待办展示会淹没
        # 真实条目，也和 Salesforce 原生首页对不上。
        me_id = self._current_user_id()

        # Date 字段用纯日期，DateTime 字段必须带时区的 ISO 时间
        # 日程取「近 7 天 ~ 远期」：只取未来会漏掉刚开完的会（这个 org 的
        # Event 实际都落在过去），只取过去又看不到排期
        min_datetime = (now - timedelta(days=30)).replace(second=0, microsecond=0).isoformat()
        max_datetime = (now + timedelta(days=window)).replace(second=0, microsecond=0).isoformat()

        if me_id:
            task_sql = str(self._settings.get("task_soql") or DEFAULT_TASK_SOQL)
            for rec in self._query(
                task_sql.format(owner=me_id, min_date=min_date, limit=max_items)
            )[:max_items]:
                items.append(self._task_item(rec))

            event_sql = str(self._settings.get("event_soql") or DEFAULT_EVENT_SOQL)
            for rec in self._query(
                event_sql.format(
                    owner=me_id,
                    min_datetime=min_datetime,
                    max_datetime=max_datetime,
                    limit=max_items,
                )
            )[:max_items]:
                items.append(self._event_item(rec))

        return items

    def _query(self, soql: str) -> list[dict[str, Any]]:
        """GET /services/data/vXX.X/query?q=... ，响应顶层平铺 records。"""
        url = f"{self._base()}/services/data/{self._api_version()}/query"
        self._ensure_session()
        resp = self._client.get(url, params={"q": soql}, headers=self._headers())
        # 会话过期：重新登录后重试一次
        if resp.status_code == 401 or "INVALID_SESSION_ID" in resp.text:
            self._authenticated = False
            self._client.cookies.clear()
            self._ensure_session()
            resp = self._client.get(url, params={"q": soql}, headers=self._headers())
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Salesforce 查询失败（{resp.status_code}）：{resp.text[:200]}"
            )
        payload = resp.json()
        if not isinstance(payload, dict):
            return []
        return list(payload.get("records") or [])

    # ---------- 首页业务板块（独立取数，不走聚合消息流） ----------

    def _current_user_id(self) -> str:
        """当前登录用户的 Id（带缓存），用于按 OwnerId / ActorId 过滤。

        优先试 /sobjects/User/me；个别 org / API 版本不支持 me 别名（实测本实例
        返回 404），回落到按 Username 反查。
        """
        if self._current_user_id_cache:
            return self._current_user_id_cache
        try:
            self._ensure_session()
            base = self._base()
            ver = self._api_version()
            headers = self._headers()
            user_id = ""
            resp = self._client.get(f"{base}/services/data/{ver}/sobjects/User/me", headers=headers)
            if resp.status_code < 400:
                user_id = str(resp.json().get("Id") or "")
            if not user_id:
                uname = str(self._settings.get("username") or "").strip().replace("'", "\\'")
                r2 = self._client.get(
                    f"{base}/services/data/{ver}/query",
                    params={"q": f"SELECT Id FROM User WHERE Username = '{uname}'"},
                    headers=headers,
                )
                if r2.status_code < 400:
                    recs = r2.json().get("records") or []
                    if recs:
                        user_id = str(recs[0].get("Id") or "")
            self._current_user_id_cache = user_id
        except Exception:  # 拿不到就空着，由调用方自己兜底
            pass
        return self._current_user_id_cache

    def fetch_today_tasks(self, limit: int = 20) -> list[dict[str, Any]]:
        """首页「今天的任务」：**我名下**今天到期 + 逾期未完成的 Task。

        口径与 Salesforce 原生首页「今天的任务」面板一致——原生按 OwnerId = 当前用户
        过滤（实测本 org 孟鹏杰名下 Task 为 0，原生面板显示"暂无待处理任务"）。
        之前这里用的是 org 全量（IsClosed=false AND ActivityDate <= TODAY），
        取回的是黄冬梅名下的 128 条同 Subject 集成失败日志，与用户看到的对不上。
        """
        me_id = self._current_user_id()
        if not me_id:
            return []
        soql = (
            "SELECT Id, Subject, ActivityDate, Status, Priority, Description, "
            "What.Name, Owner.Name FROM Task "
            "WHERE IsClosed = false AND OwnerId = '{owner}' AND ActivityDate <= TODAY "
            "ORDER BY ActivityDate ASC NULLS LAST LIMIT {limit}"
        ).format(owner=me_id, limit=int(limit))
        items: list[dict[str, Any]] = []
        for rec in self._query(soql):
            activity = str(rec.get("ActivityDate") or "")
            items.append({
                "id": str(rec.get("Id") or ""),
                "subject": str(rec.get("Subject") or "").strip() or "Salesforce 任务",
                "activity_date": activity,
                "status": _TASK_STATUS.get(str(rec.get("Status") or "").lower(), rec.get("Status") or "未开始"),
                "priority": normalize_priority(rec.get("Priority")),
                "what_name": _safe_name(rec.get("What")),
                "owner_name": _safe_name(rec.get("Owner")),
                "url": f"{self._base()}/{rec.get('Id')}" if rec.get("Id") else "",
            })
        return items

    def fetch_my_events(self, limit: int = 10) -> list[dict[str, Any]]:
        """首页「今日日程」：**我名下**今天及以后结束的 Event。

        对应 Salesforce 原生首页「今日事件」面板（原生按 EndDateTime 落在今天及以后
        取前几条）。实测本 org 原生面板显示的 10/21、10/22 两条，与我名下 Event 的
        结束时间一一对应。

        时间统一换算成东八区再返回：这个 org 的 Event 结束时间都落在本地 23:59
        （UTC 15:59），直接回传 UTC 会让前端显示成 15:59，和原生面板对不上。
        """
        me_id = self._current_user_id()
        if not me_id:
            return []
        soql = (
            "SELECT Id, Subject, StartDateTime, EndDateTime, IsAllDayEvent, Location, "
            "Description FROM Event "
            "WHERE OwnerId = '{owner}' AND EndDateTime >= {today} "
            "ORDER BY EndDateTime ASC LIMIT {limit}"
        ).format(owner=me_id, today=_today_iso(), limit=int(limit))
        items: list[dict[str, Any]] = []
        for rec in self._query(soql):
            items.append({
                "id": str(rec.get("Id") or ""),
                "subject": str(rec.get("Subject") or "").strip() or "Salesforce 日程",
                "start": _to_local_iso(rec.get("StartDateTime")),
                "end": _to_local_iso(rec.get("EndDateTime")),
                "all_day": bool(rec.get("IsAllDayEvent")),
                "location": str(rec.get("Location") or "").strip(),
                "description": str(rec.get("Description") or "").strip(),
                "url": f"{self._base()}/{rec.get('Id')}" if rec.get("Id") else "",
            })
        return items

    def fetch_my_approvals(self, limit: int = 20) -> list[dict[str, Any]]:
        """首页「要批准的项目」：指派给当前登录人的待审批工作项（ProcessInstanceWorkitem）。

        通过 ProcessInstance.TargetObject 关系拿到被审批对象的 Name / Type，
        这样卡片上能直接显示「FB-261012467（Feedback）」而不是一串 Id。

        拿到 Type 后再按**单据类型**分发取数：不同单据类型承载信息的字段不同，
        统一只显示单据号没有意义。详见 _approval_detail_fields。
        """
        me_id = self._current_user_id()
        if not me_id:
            return []
        soql = (
            "SELECT Id, ProcessInstanceId, ActorId, CreatedDate, "
            "ProcessInstance.TargetObjectId, ProcessInstance.TargetObject.Name, "
            "ProcessInstance.TargetObject.Type FROM ProcessInstanceWorkitem "
            "WHERE ActorId = '{actor}' ORDER BY CreatedDate DESC LIMIT {limit}"
        ).format(actor=me_id, limit=int(limit))
        items: list[dict[str, Any]] = []
        for rec in self._query(soql):
            proc = rec.get("ProcessInstance") or {}
            target = proc.get("TargetObject") or {}
            target_id = str(proc.get("TargetObjectId") or "")
            target_type = str(target.get("Type") or "").strip()
            item = {
                "id": str(rec.get("Id") or ""),
                "target_id": target_id,
                "target_name": str(target.get("Name") or "").strip() or "待审批对象",
                "target_type": target_type,
                "created_date": str(rec.get("CreatedDate") or ""),
                "url": f"{self._base()}/{target_id}" if target_id else "",
                "doc_type": "",
                "fields": [],
            }
            if target_id:
                detail = self._approval_detail_fields(target_type, target_id)
                item["doc_type"] = detail["doc_type"]
                item["fields"] = detail["fields"]
            items.append(item)
        return items

    def _approval_detail_fields(self, target_type: str, target_id: str) -> dict[str, Any]:
        """按单据类型取出该单据该展示的字段。

        **通用三件套**（所有 Salesforce 对象都有这几个标准字段，实测 20+ 种
        业务对象全部可用，所以不必为每种单据写分支）：
          单据类型 = 对象的业务名（label，如「客户反馈/回访」）
          创建人   = CreatedBy.Name（谁创建了这张单据）
          创建日期 = CreatedDate

        再按类型追加**该单据特有的**业务字段（目前只有 Feedback__c 需要）。

        关于"创建人"：取的是**创建单据的人**（单据的 CreatedBy），不是
        "提交审批的人"（工作项的 CreatedBy）——本例中两者不同：
        孟鹏杰创建了单据，邱宇提交了审批。
        """
        if not target_type:
            return {"doc_type": "", "fields": []}

        doc_type = self._object_label(target_type)
        fields: list[dict[str, str]] = []
        if doc_type:
            fields.append({"label": "单据类型", "value": doc_type})
        if not target_id:
            # 拿不到具体记录时，至少还能报出单据类型
            return {"doc_type": doc_type, "fields": fields}

        # 创建人 / 创建日期：标准字段，所有对象通用
        creator, created = self._record_creator(target_type, target_id)
        if creator:
            fields.append({"label": "创建人", "value": creator})
        if created:
            fields.append({"label": "创建日期", "value": created})

        # 该单据特有的业务字段
        if target_type == "Feedback__c":
            fields.extend(self._feedback_specific_fields(target_id))

        return {"doc_type": doc_type, "fields": fields}

    def _object_label(self, target_type: str) -> str:
        """取对象的业务名称（label），作为「单据类型」。

        实测 Feedback__c 的 label 是「客户反馈/回访」，而它的记录类型有
        「客诉反馈」「反馈明细」「服务回访单」等 15 种。用户口中的"客户反馈/回访"
        指的就是对象名，所以这里用 label 而不是记录类型名。
        """
        cached = _OBJECT_LABEL_CACHE.get(target_type)
        if cached is not None:
            return cached
        label = ""
        try:
            # 必须先确保有会话：_headers() 在 totp 模式下要取 sid cookie，
            # 没会话时它会直接抛异常（被下面吞掉 → label 变空）。
            self._ensure_session()
            url = f"{self._base()}/services/data/{self._api_version()}/sobjects/{target_type}/describe"
            resp = self._client.get(url, headers=self._headers())
            if resp.status_code < 400:
                label = str(resp.json().get("label") or "").strip()
        except Exception:  # noqa: BLE001
            label = ""
        # 失败不缓存：会话可能随后恢复，下次还能再取一次
        if label:
            _OBJECT_LABEL_CACHE[target_type] = label
        return label

    def _record_creator(self, target_type: str, target_id: str) -> tuple[str, str]:
        """取单据的创建人与创建日期（标准字段，所有对象都有）。"""
        rows = self._query(
            "SELECT CreatedBy.Name, CreatedDate FROM {obj} WHERE Id = '{rid}' LIMIT 1".format(
                obj=target_type, rid=target_id
            )
        )
        if not rows:
            return "", ""
        rec = rows[0]
        creator = str((rec.get("CreatedBy") or {}).get("Name") or "").strip()
        created = _slash_date(str(rec.get("CreatedDate") or "")[:10])
        return creator, created

    def _feedback_specific_fields(self, target_id: str) -> list[dict[str, str]]:
        """Feedback__c 专有字段：终端客户 / 二级-三级分类 / 反馈日期。

        该对象有父子两层结构：主单（记录类型「客诉反馈」）带 **终端客户**
        （Account__r.Name）与 **反馈日期**（FeedbackDate__c）；**二级-三级分类**
        （Category2__c/Category3__c）填在子记录（记录类型「反馈明细」）上，
        子记录通过自引用字段 BackMain__c 指向主单。实测 FB-261012467
        （绵阳市人民医院 / 2026-10-13）挂了 2 条明细，其中一条是「品牌推广-视频拍摄」
        ——正好拼成用户要的那一行。
        """
        rows = self._query(
            "SELECT RecordType.Name, Account__r.Name, Category1__c, "
            "Category2__c, Category3__c, FeedbackDate__c, Status__c "
            "FROM Feedback__c WHERE Id = '{rid}' LIMIT 1".format(rid=target_id)
        )
        if not rows:
            return []
        rec = rows[0]
        record_type = str((rec.get("RecordType") or {}).get("Name") or "").strip()
        customer = str((rec.get("Account__r") or {}).get("Name") or "").strip()
        category2 = str(rec.get("Category2__c") or "").strip()
        category3 = str(rec.get("Category3__c") or "").strip()
        feedback_date = str(rec.get("FeedbackDate__c") or "").strip()
        status = str(rec.get("Status__c") or "").strip()

        # 二三级分类常填在子记录（反馈明细）上，用自引用字段 BackMain__c 找主单的明细
        if not category2:
            details = self._query(
                "SELECT Category2__c, Category3__c FROM Feedback__c "
                "WHERE BackMain__c = '{rid}' ORDER BY CreatedDate ASC LIMIT 20".format(rid=target_id)
            )
            pairs: list[tuple[str, str]] = []
            for d in details:
                c2 = str(d.get("Category2__c") or "").strip()
                c3 = str(d.get("Category3__c") or "").strip()
                if c2 or c3:
                    pairs.append((c2, c3))
            category2 = pairs[0][0] if pairs else ""
            category3 = pairs[0][1] if pairs else ""

        fields: list[dict[str, str]] = []
        if customer:
            fields.append({"label": "终端客户", "value": customer})
        if category2 or category3:
            fields.append({
                "label": "分类",
                "value": " - ".join(x for x in (category2, category3) if x),
            })
        if feedback_date:
            fields.append({"label": "反馈日期", "value": _slash_date(feedback_date)})
        if record_type:
            fields.append({"label": "回访单类型", "value": record_type})
        if status:
            fields.append({"label": "状态", "value": status})
        return fields

    def fetch_my_weekly_plans(self, limit: int = 10) -> list[dict[str, Any]]:
        """首页「销售周计划」：当前登录人名下（OwnerId）的周计划（Weekly_Plan__c）。"""
        me_id = self._current_user_id()
        if not me_id:
            return []
        soql = (
            "SELECT Id, Name, WeekName__c, Start_Date__c, End_Date__c, Status__c, "
            "Company__c, Manager__c, Business_Total__c, CreatedDate FROM Weekly_Plan__c "
            "WHERE OwnerId = '{owner}' ORDER BY CreatedDate DESC LIMIT {limit}"
        ).format(owner=me_id, limit=int(limit))
        items: list[dict[str, Any]] = []
        for rec in self._query(soql):
            items.append({
                "id": str(rec.get("Id") or ""),
                "name": str(rec.get("Name") or "").strip(),
                "week_name": str(rec.get("WeekName__c") or "").strip(),
                "start_date": str(rec.get("Start_Date__c") or ""),
                "end_date": str(rec.get("End_Date__c") or ""),
                "status": str(rec.get("Status__c") or "").strip(),
                "company": str(rec.get("Company__c") or "").strip(),
                "manager": str(rec.get("Manager__c") or "").strip(),
                "business_total": rec.get("Business_Total__c"),
                "url": f"{self._base()}/{rec.get('Id')}" if rec.get("Id") else "",
            })
        return items

    # ---------- 归一化 ----------

    def _task_item(self, rec: dict[str, Any]) -> MessageItem:
        subject = str(rec.get("Subject") or "").strip() or "Salesforce 待办"
        what = rec.get("What") or {}
        what_name = str(what.get("Name") or "").strip() if isinstance(what, dict) else ""
        # 实测这个 org 的 Task 是「同一条业务规则 × N 台仪器」，
        # 30 条的 Subject 完全一样，只有 What（仪器资产串）能区分。
        # 所以标题用医院名，任务名放进 detail，否则首页就是 30 行重复文字。
        hospital = _hospital_of(what_name)
        status_raw = str(rec.get("Status") or "").strip()
        status = _TASK_STATUS.get(status_raw.lower(), status_raw or "未开始")
        detail_bits = [f"Salesforce 待办 · {status}"]
        if subject:
            detail_bits.append(subject)
        if what_name:
            detail_bits.append(what_name[:90])
        owner = rec.get("Owner") or {}
        owner_name = str(owner.get("Name") or "").strip() if isinstance(owner, dict) else ""
        desc = str(rec.get("Description") or "").strip()
        if desc:
            detail_bits.append(desc[:60])
        return MessageItem(
            source=SOURCE_SALESFORCE,
            external_id=str(rec.get("Id") or ""),
            title=hospital or subject,
            detail=" · ".join(detail_bits),
            due=format_due(rec.get("ActivityDate")),
            priority=normalize_priority(rec.get("Priority")),
            url=f"{self._base()}/{rec.get('Id')}" if rec.get("Id") else "",
            category="待办",
            kind="task",
            creator=owner_name,
            extra={
                "kind": "task",
                "status": status_raw,
                "what": what_name,
                "owner": owner_name,
            },
        )

    def _event_item(self, rec: dict[str, Any]) -> MessageItem:
        subject = str(rec.get("Subject") or "").strip() or "Salesforce 日程"
        all_day = bool(rec.get("IsAllDayEvent"))
        location = str(rec.get("Location") or "").strip()
        desc = str(rec.get("Description") or "").strip()
        # 时间统一换算到东八区再显示，否则本地 23:59 的日程会显示成 15:59
        start = _to_local_iso(rec.get("StartDateTime"))
        end = _to_local_iso(rec.get("EndDateTime"))
        detail_bits = ["Salesforce 日程"]
        if location:
            detail_bits.append(f"地点 {location}")
        if not all_day and end:
            end_text = str(end)
            if len(end_text) >= 16:
                detail_bits.append(f"至 {end_text[11:16]}")
        if desc:
            detail_bits.append(desc[:80])
        return MessageItem(
            source=SOURCE_SALESFORCE,
            external_id=str(rec.get("Id") or ""),
            title=subject,
            detail=" · ".join(detail_bits),
            due=format_due(start, all_day=all_day),
            priority=PRIORITY_MEDIUM,
            url=f"{self._base()}/{rec.get('Id')}" if rec.get("Id") else "",
            category="日程",
            kind="event",
            extra={"kind": "event", "all_day": all_day, "start": str(start)},
        )

    # ---------- 状态 ----------

    def status(self) -> dict[str, object]:
        """给首页来源角标用：说明这一家当前处于什么状态。

        enabled 只表示"凭据齐备、能取数"；reason 用来区分
        「未配置」（enabled=False）与「已配置但会话失效」（enabled=True + reason）。
        后者必须靠真实取数报错才知道，所以这里要把 _error 透出去，
        否则会话过期后首页仍显示"已连接"，用户会以为一切正常。
        """
        enabled = self.configured()
        reason = ""
        if not enabled:
            reason = self._unconfigured_reason()
        elif self._error:
            reason = self._error
        return {
            "source": SOURCE_SALESFORCE,
            "enabled": enabled,
            "reason": reason,
            "error": self._error,
        }

    def _unconfigured_reason(self) -> str:
        s = self._settings
        if not s.get("instance_url"):
            return "尚未配置实例地址（instance_url）"
        mode = str(s.get("auth_mode") or "totp").strip()
        if mode == "oauth":
            return "OAuth 模式还需 client_id + client_secret（或 refresh_token）"
        if mode == "session":
            return "会话模式需要填会话 id（access_token）"
        # totp 模式：缺什么就说什么。6 位动态码是登录时手输的一次性凭证，
        # 不落盘，所以这里只提示账号/密码——别再要求用户去填 base32 密钥，
        # 那个字段对本项目的登录流程并不是必需品。
        missing = [
            name
            for name, key in (("用户名", "username"), ("密码", "password"))
            if not s.get(key)
        ]
        if missing:
            return f"还缺：{'、'.join(missing)}"
        # 账号密码都有但还没有会话：需要用 6 位动态码登录一次
        return "还缺：动态口令（腾讯验证器 6 位码）或已保存的会话"


# ---------- 板块批量取数（供首页看板与消息聚合共用） ----------

# 首页四个板块的取数入口
_BOARD_FETCHERS: dict[str, str] = {
    "today_tasks": "fetch_today_tasks",
    "my_events": "fetch_my_events",
    "approvals": "fetch_my_approvals",
    "weekly_plans": "fetch_my_weekly_plans",
}


def fetch_boards_cached() -> dict[str, Any]:
    """一次取好四个板块（并发），结果进共享缓存。

    首页一轮轮询会并发请求这四个接口。若各自独立取数，一轮就是 4~5 次
    上游调用（实测整轮 11.5 秒），配额扛不住。这里改成：
    任一板块发现缓存过期 → 一次性把四个都取好 → 本轮内其它板块直接命中。

    返回 {板块名: 数据}，数据格式与各 fetch_* 方法一致（items 为 dict 列表）。
    """
    # 全部命中缓存就直接返回，不再打上游
    cached_all = {key: _board_cache_get(key) for key in _BOARD_FETCHERS}
    if all(v is not None for v in cached_all.values()):
        return {k: v for k, v in cached_all.items() if v is not None}

    adapter = SalesforceAdapter()
    if not adapter.configured():
        empty: dict[str, Any] = {}
        for key in _BOARD_FETCHERS:
            data = {"instance_url": "", "connected": False, "error": "", "items": []}
            _board_cache_put(key, data)
            empty[key] = data
        return empty

    def _one(key: str, method: str) -> tuple[str, Any]:
        try:
            items = getattr(adapter, method)()
            return key, {
                "instance_url": str(adapter._settings.get("instance_url") or ""),
                "connected": True,
                "error": "",
                "items": items,
            }
        except Exception as exc:  # noqa: BLE001
            return key, {
                "instance_url": str(adapter._settings.get("instance_url") or ""),
                "connected": False,
                "error": str(exc),
                "items": [],
            }

    result: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=len(_BOARD_FETCHERS)) as pool:
        futures = [
            pool.submit(_one, key, method)
            for key, method in _BOARD_FETCHERS.items()
        ]
        for future in futures:
            key, data = future.result()
            _board_cache_put(key, data)
            result[key] = data
    return result
