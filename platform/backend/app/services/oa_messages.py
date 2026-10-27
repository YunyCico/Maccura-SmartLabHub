"""公司 OA 消息适配器（泛微 e-cology）。

实测 oa.maccura.com 的结论，决定了这里的写法：
- 系统是泛微 e-cology（Cookie 名 ecology_JSessionId，登录页 /login/Login.jsp）
- 所有 /api/* 都返回同一个 {"errorCode":"001","status":false}（用户信息为空），
  无法通过状态码区分真实端点和虚构端点

因此端点路径、鉴权方式、字段映射一律走 .env 配置，不写死。
不同 e-cology 版本的待办路径和返回结构差异很大，写死必错。

支持两种鉴权：
- password：拿账号密码换会话 Cookie（默认）
- token：直接带固定 token / Cookie 串
"""

from __future__ import annotations

import logging
import re
from typing import Any
import html as html_lib
import time
import xml.etree.ElementTree as ET

import httpx

from app.services import integration_config
from app.services.message_models import (
    PRIORITY_MEDIUM,
    SOURCE_OA,
    MessageItem,
    format_due,
    normalize_priority,
)

logger = logging.getLogger(__name__)

# 泛微待办列表页里那一行：var __tableStringKey__='<32位HEX>';
# 这个 key 每次渲染都不同，必须现取现用。
_TABLE_STRING_RE = re.compile(r"__tableStringKey__\s*=\s*['\"]([0-9A-Fa-f]{32})['\"]")
# 必须伪装成浏览器：企业网关会按 UA 拦非浏览器客户端（实测 SmartLabHub/1.0 直接 404）
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)
DEFAULT_XML_PATH = "weaver/weaver.common.util.taglib.SplitPageXmlServlet"
DEFAULT_PAGE_ID = "Wf:pendingMatters"


class OaAdapter:
    def __init__(self) -> None:
        # 读集成配置文件优先于 .env，设置页保存后立即生效
        self._settings = integration_config.effective_settings()["oa"]
        # 最近一次取数的错误原因，供 status() 透出（首页角标/看板用）
        self._error = ""

    def configured(self) -> bool:
        s = self._settings
        if not s["base_url"]:
            return False
        if s["auth_mode"] == "token":
            return bool(s["token"])
        return bool(s["username"] and s["password"])

    @property
    def error(self) -> str:
        return self._error

    def unconfigured_reason(self) -> str:
        """没配好时说明还缺什么。"""
        s = self._settings
        missing: list[str] = []
        if not s["base_url"]:
            missing.append("系统地址")
        if s["auth_mode"] == "token":
            if not s["token"]:
                missing.append("Token")
        else:
            if not s["username"]:
                missing.append("登录账号")
            if not s["password"]:
                missing.append("登录密码")
        if not s["todo_path"]:
            missing.append("待办接口路径")
        if not s["field_map"]:
            missing.append("字段映射")
        if missing:
            return f"还缺：{'、'.join(missing)}"
        return ""

    def status(self) -> dict[str, Any]:
        """给首页角标用：说明 OA 当前处于什么状态。"""
        enabled = self.configured()
        reason = "" if enabled else self.unconfigured_reason()
        if enabled and self._error:
            reason = self._error
        return {
            "source": SOURCE_OA,
            "enabled": enabled,
            "reason": reason,
            "error": self._error,
        }

    # ---------- 鉴权 ----------

    def _auth_headers(self) -> dict[str, str]:
        s = self._settings
        # ★ 必须伪装成浏览器 UA。
        #   实测 oa.maccura.com：同样路径同样 cookie，用 "SmartLabHub/1.0" 请求
        #   会被 Resin 直接回 404（"was not found on this server"），
        #   换成 Chrome UA 就正常返回 XML。企业网关按 UA 拦非浏览器客户端。
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "User-Agent": _BROWSER_UA,
        }
        if s["auth_mode"] == "token":
            if s["auth_header"]:
                headers[s["auth_header"]] = s["token"]
            else:
                headers["Cookie"] = s["token"]
        return headers

    def _login(self, client: httpx.Client) -> bool:
        """用账号密码登录，登录态存在 client 的 cookie jar 里。

        泛微 e-cology 8 的实际字段是 loginid / userpassword / logintype
        （不是通用写法里的 loginId / password），且表单要求带上
        rnd / serial 这类隐藏字段——它们是前端 JS 在提交时填的随机值，
        这里自己造一个即可。
        """
        # ★ token 模式（直接带 Cookie）不需要登录，直接放行。
        #   少了这一句，token 模式也会去跑登录流程，而配置里没有账号密码 → 取数全挂。
        if str(self._settings.get("auth_mode") or "") != "password":
            return True
        ok, _detail = self._do_login(client)
        return ok

    def login_and_get_cookie(self) -> tuple[str, str]:
        """登录一次并导出会话 cookie 串，供设置页「登录」按钮用。

        返回 (cookie串, 错误说明)；成功时错误说明为空。
        """
        s = self._settings
        if not s["base_url"]:
            return "", "还没填系统地址"
        if not s["username"] or not s["password"]:
            return "", "还缺：登录账号 / 密码"
        with httpx.Client(follow_redirects=True) as client:
            ok, detail = self._do_login(client)
            if not ok:
                return "", detail
            pairs = [f"{c.name}={c.value}" for c in client.cookies.jar]
            return "; ".join(pairs), ""

    def _do_login(self, client: httpx.Client) -> tuple[bool, str]:
        """真正干活的登录，返回 (是否成功, 失败原因)。

        判据（实测）：
          · 成功：cookie 里出现 ecology_JSessionid，或被重定向离开 Login.jsp
          · 失败：仍停在 Login.jsp，且页面里有错误提示文字
        """
        s = self._settings
        base = s["base_url"].rstrip("/")
        login_path = s["login_path"] or "login/Login.jsp?logintype=1"
        login_url = f"{base}/{login_path.lstrip('/')}"

        # 先取登录页：既建立会话，也把页面上的表单字段整份抄下来
        try:
            page = client.get(login_url, headers={"User-Agent": _BROWSER_UA}, timeout=15)
        except httpx.HTTPError as exc:
            return False, f"OA 登录页打不开：{exc}"

        # ★ 照抄页面上的全部 input，只覆盖账号密码。
        #   实测（2026-10-27 抓浏览器真实请求）必须带：
        #     loginfile=/wui/theme/ecology8/page/login2.jsp?templateId=21&logintype=1&gopage=
        #     fontName=微软雅黑 / submit=登录 / logintype=1 / isie=false / islanguid=7
        #   少一个就登不进去。硬编码容易随版本变，直接从页面上读最稳。
        payload = _extract_form_fields(page.text)
        payload["loginid"] = s["username"]
        payload["userpassword"] = s["password"]
        payload.setdefault("logintype", "1")
        # submit 按钮在 HTML 里是 "SIGNIN"，页面 JS 会按语言改成中文；浏览器实际提交的是「登录」
        if str(payload.get("submit", "")).upper() in ("SIGNIN", ""):
            payload["submit"] = "登录"
        # rnd / serial 在真实请求里就是空的
        payload["rnd"] = ""
        payload["serial"] = ""
        try:
            resp = client.post(
                f"{base}/login/VerifyLogin.jsp",
                data=payload,
                headers={
                    "User-Agent": _BROWSER_UA,
                    "Referer": login_url,
                    "Origin": base,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=20,
            )
        except httpx.HTTPError as exc:
            return False, f"OA 登录请求失败：{exc}"
        if resp.status_code >= 400:
            return False, f"OA 登录返回 HTTP {resp.status_code}"

        names = {c.name.lower() for c in client.cookies.jar}

        # ★ 登录成功的判据：出现 yili_userid / loginidweaver 这两个 cookie。
        #   实测（2026-10-27）：泛微登录成功后**不返回 302**，而是 200 + 空 body，
        #   靠前端 JS 自己跳转 —— 所以「最终 URL 是否离开 Login.jsp」判断不了成功，
        #   之前就是因为这个误判成失败，明明已经登进去了。
        #   失败时只会拿到 ecology_JSessionId / JSESSIONID，不会有 yili_userid。
        if names & {"yili_userid", "loginidweaver"}:
            return True, ""

        # 失败：给一句两种可能都覆盖的提示，别把用户带偏
        text = _plain_text(resp.text)
        for keyword in ("用户不存在", "用户被锁定", "密码错误", "用户名或密码"):
            if keyword in text:
                return False, f"登录被拒：{keyword}"
        return False, (
            "登录未通过：账号或密码不正确。"
            "OA 的账号通常是工号（纯数字），不是姓名；"
            "如果密码确认无误，也可能是该账号启用了动态令牌"
        )

    # ---------- 拉取 ----------

    def fetch(self) -> list[MessageItem]:
        s = self._settings
        self._error = ""
        if not self.configured() or not s["todo_path"]:
            return []
        try:
            with httpx.Client(follow_redirects=True) as client:
                if not self._login(client):
                    self._error = "OA 登录失败，请检查账号密码或登录路径"
                    return []
                headers = self._auth_headers()
                if str(s.get("parser") or "json") == "weaver_xml":
                    return self._fetch_weaver_xml(client, headers)
                resp = client.get(
                    f"{s['base_url'].rstrip('/')}/{s['todo_path'].lstrip('/')}",
                    headers=headers,
                    timeout=float(s["timeout_seconds"]),
                    **_query_kwargs(s["todo_query_params"]),
                )
        except httpx.HTTPError as exc:
            logger.warning("OA 待办拉取失败：%s", exc)
            self._error = f"OA 待办拉取失败：{exc}"
            return []
        if resp.status_code >= 400:
            logger.warning("OA 待办返回 HTTP %s", resp.status_code)
            self._error = f"OA 待办接口返回 HTTP {resp.status_code}"
            return []
        payload = _parse_json(resp.text)
        if payload is None:
            logger.warning("OA 待办返回非 JSON，需检查待办路径与字段映射配置")
            self._error = "OA 待办返回非 JSON，请检查待办路径与字段映射配置"
            return []
        items = self._map_rows(payload)
        if not items and not self._error:
            self._error = "OA 已连通，但没有解析出待办（检查字段映射与数据行路径）"
        return items

    # ---------- 泛微 e-cology 表格 XML ----------

    def _fetch_weaver_xml(
        self, client: httpx.Client, headers: dict[str, str]
    ) -> list[MessageItem]:
        """泛微 e-cology 的待办列表：两步取值。

        实测（2026-10-27）：
          第一步 GET 待办列表页（WFSearchResult.jsp），HTML 里有一行
            `var __tableStringKey__='<32位HEX>'`
          第二步 GET SplitPageXmlServlet?...&tableString=<那个HEX>&pageIndex=0
            → 返回 <table recordCount="205"><head><col .../></head><row>...</row></table>

        为什么要两步：tableString 是服务端每次渲染时新生成的表格会话 key，
        不能自己造也不能复用（每次都不一样），必须从列表页里抠出来。
        """
        s = self._settings
        base = s["base_url"].rstrip("/")
        timeout = float(s["timeout_seconds"])

        # ① 拿 tableString
        try:
            list_resp = client.get(
                f"{base}/{s['todo_path'].lstrip('/')}",
                headers=headers,
                timeout=timeout,
                **_query_kwargs(s["todo_query_params"]),
            )
        except httpx.HTTPError as exc:
            self._error = f"OA 待办列表页打不开：{exc}"
            return []
        if list_resp.status_code >= 400:
            self._error = f"OA 待办列表页返回 HTTP {list_resp.status_code}"
            return []
        match = _TABLE_STRING_RE.search(list_resp.text)
        if not match:
            self._error = (
                "OA 待办列表页里没找到 tableString，"
                "通常说明登录态无效（返回的是登录页），请检查账号密码"
            )
            return []
        table_string = match.group(1)

        # ② 用 tableString 取 XML
        xml_path = s.get("xml_path") or DEFAULT_XML_PATH
        params = {
            "tableInstanceId": "",
            "tableString": table_string,
            "pageIndex": "0",
            "orderBy": "null",
            "otype": "null",
            "mode": "run",
            "customParams": "null",
            "selectedstrs": "",
            "pageId": s.get("page_id") or DEFAULT_PAGE_ID,
        }
        # ★ 这一步**必须带 Referer**（指向待办列表页）。
        #   实测：同样的 URL、同样的 cookie，不带 Referer 时 Resin 直接回 404
        #   「SplitPageXmlServlet was not found on this server」——
        #   是防盗链式校验，不是路径写错。
        list_url = f"{base}/{s['todo_path'].lstrip('/')}"
        xml_headers = {**headers, "Referer": list_url}
        try:
            xml_resp = client.get(
                f"{base}/{xml_path.lstrip('/')}",
                headers=xml_headers,
                params=params,
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            self._error = f"OA 待办数据接口请求失败：{exc}"
            return []
        if xml_resp.status_code == 404:
            self._error = (
                "OA 待办数据接口返回 404。这个接口校验来源页，"
                "请确认「待办列表页路径」填的是浏览器里待办页的地址"
            )
            return []
        if xml_resp.status_code >= 400:
            self._error = f"OA 待办数据接口返回 HTTP {xml_resp.status_code}"
            return []
        return self._parse_weaver_xml(xml_resp.text)

    def _parse_weaver_xml(self, text: str) -> list[MessageItem]:
        """把泛微的表格 XML 转成消息条目。

        行结构（每行是一个 <row>，里面按列的 column 属性给值）：
            <row>
              <col column="requestname" linkvalue="5336870" value="会议通知: ..."/>
              <col column="creater"     value="15824"/>
              <col column="createdate"  value="2026-10-19"/>
            </row>
        """
        s = self._settings
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            self._error = f"OA 待办 XML 解析失败：{exc}"
            return []

        total = root.get("recordCount") or "0"
        max_items = int(s["max_items"] or 30)
        default_title = s["default_title"] or "OA 待办"
        field_map = _parse_field_map(s["field_map"])

        items: list[MessageItem] = []
        for row in root.findall("row"):
            values: dict[str, str] = {}
            request_id = ""
            for col in row.findall("col"):
                column = col.get("column") or ""
                if column:
                    values[column] = html_lib.unescape(col.get("value") or "")
                # 详情页要的是 requestid：标题列带 linkvalue，勾选列也带
                if column == "requestname":
                    request_id = col.get("linkvalue") or ""

            title = _pick(values, field_map.get("title") or "requestname")
            if not title:
                continue
            title = re.sub(r"\s+", " ", title).strip()
            request_id = request_id or _pick(values, field_map.get("id") or "requestid", "")
            creator = _pick(values, field_map.get("creator", ""), "")
            detail_bits = [f"OA 待办 · 共 {total} 条"]
            if creator and not creator.isdigit():
                detail_bits.insert(1, creator)
            items.append(
                MessageItem(
                    source=SOURCE_OA,
                    external_id=request_id,
                    title=title,
                    detail=" · ".join(detail_bits),
                    due=format_due(_pick(values, field_map.get("due") or "createdate", "")),
                    priority=PRIORITY_MEDIUM,
                    url=self._request_url(request_id) if request_id else "",
                    category=default_title,
                    creator="" if creator.isdigit() else creator,
                    extra={"kind": "oa_todo", "total": total},
                )
            )
            if len(items) >= max_items:
                break
        if not items and not self._error:
            self._error = (
                f"OA 已连通，待办共 {total} 条，但这一页没解析出条目"
                "（检查字段映射里的 title 是否对得上 XML 的 column）"
            )
        return items

    def _request_url(self, request_id: str) -> str:
        """拼详情页链接。泛微的请求详情页固定是 ViewRequest.jsp?requestid=..."""
        base = self._settings["base_url"].rstrip("/")
        if not base:
            return ""
        return f"{base}/workflow/request/ViewRequest.jsp?requestid={request_id}&isovertime=0"

    def _map_rows(self, payload: Any) -> list[MessageItem]:
        """按配置的路径取值，再用字段映射表归一化。"""
        s = self._settings
        rows: Any = payload
        for path in [p.strip() for p in str(s["rows_path"]).split(",") if p.strip()]:
            rows = dig(rows, path)
            if rows is None:
                return []
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            return []
        field_map = _parse_field_map(s["field_map"])
        # 没有配字段映射就无法定位标题，宁可返回空也不要造出一堆无标题的假待办
        if not field_map.get("title"):
            logger.warning("OA 未配置字段映射中的 title，跳过解析")
            return []
        max_items = int(s["max_items"] or 30)
        default_title = s["default_title"] or "OA 待办"
        items = []
        for row in rows[:max_items]:
            if not isinstance(row, dict):
                continue
            title = _pick(row, field_map["title"])
            if not title:
                continue
            creator = _pick(row, field_map.get("creator", ""), "")
            detail_bits = [f"OA 待办 · 发起人 {creator}" if creator else "OA 待办"]
            items.append(
                MessageItem(
                    source=SOURCE_OA,
                    external_id=_pick(row, field_map.get("id", ""), ""),
                    title=title,
                    detail=" · ".join(detail_bits),
                    due=format_due(_pick(row, field_map.get("due", ""), "")),
                    priority=normalize_priority(_pick(row, field_map.get("priority", ""), "")),
                    url=_pick(row, field_map.get("url", ""), ""),
                    category=default_title,
                    creator=creator,
                    extra={"kind": "oa_todo"},
                )
            )
        return items


# ---------- 解析辅助 ----------


def dig(data: Any, path: str) -> Any:
    """按 a.b[0].c 取值，任何一层缺失返回 None。

    每段可写成 "name" 或 "name[3]"：先按 name 取值，若带索引再按下标取一次。
    这样 result.todos[1].subject 能正确走到"第 2 条待办的 subject"。
    """
    current = data
    if not path:
        return current
    for segment in path.split("."):
        name = re.sub(r"\[\d+\]$", "", segment)
        index_match = re.search(r"\[(\d+)\]$", segment)

        if isinstance(current, dict):
            if not name:
                return None
            current = current.get(name)
            if current is None:
                return None

        if index_match:
            if not isinstance(current, list):
                return None
            idx = int(index_match.group(1))
            if idx >= len(current) or idx < -len(current):
                return None
            current = current[idx]
        elif not name and not isinstance(current, list):
            # 段名被正则剥空后又没有索引，说明路径写法有问题
            return None
    return current


def _extract_form_fields(html: str) -> dict[str, str]:
    """把页面上所有 <input> 的 name/value 抄成 dict。

    用途：泛微登录表单有一堆隐藏字段（loginfile / fontName / isie / islanguid …），
    少一个就登不进去，而且各版本还不一样。与其硬编码，不如照页面上的抄。
    """
    fields: dict[str, str] = {}
    for match in re.finditer(r"<input\b[^>]*>", html, re.I):
        tag = match.group(0)
        name = re.search(r"name\s*=\s*[\"']?([^\"'\s>]+)", tag, re.I)
        if not name:
            continue
        value = re.search(r"value\s*=\s*[\"']([^\"']*)[\"']", tag, re.I)
        fields[name.group(1)] = html_lib.unescape(value.group(1)) if value else ""
    return fields


def _plain_text(html: str) -> str:
    """把登录失败页的 HTML 剥成纯文本，用来抠服务端的错误提示。"""
    if not html:
        return ""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


def _pick(row: dict[str, Any], path: str, default: str = "") -> str:
    if not path:
        return default
    value = dig(row, path)
    if value in (None, "", [], {}):
        return default
    return str(value)


def _parse_json(text: str) -> Any:
    import json

    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _query_kwargs(text: Any) -> dict[str, Any]:
    """把查询参数包成 httpx 的 kwargs。

    ★ 空参数时**必须不传 params**：httpx 收到 params={} 会把 URL 里
      原有的查询串整个替换掉（实测：...?resourceid=15824 变成光秃秃的 .jsp），
      于是拿回来的是另一个页面，怎么都解析不出数据。
    """
    query = _parse_query(text)
    return {"params": query} if query else {}


def _parse_query(text: Any) -> dict[str, str]:
    """把 "a=1&b=2" 解析成 dict。"""
    result: dict[str, str] = {}
    for chunk in str(text or "").split("&"):
        if "=" in chunk:
            key, value = chunk.split("=", 1)
            if key.strip():
                result[key.strip()] = value.strip()
    return result


def _parse_field_map(text: Any) -> dict[str, str]:
    """解析字段映射 JSON；写坏时返回空 dict 而不是崩。"""
    raw = str(text or "").strip()
    if not raw:
        return {}
    import json

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("OA 字段映射不是合法 JSON，请检查配置")
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
