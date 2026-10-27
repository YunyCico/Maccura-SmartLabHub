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
        headers = {"Accept": "application/json", "User-Agent": "SmartLabHub/1.0"}
        if s["auth_mode"] == "token":
            if s["auth_header"]:
                headers[s["auth_header"]] = s["token"]
            else:
                headers["Cookie"] = s["token"]
        return headers

    def _login(self, client: httpx.Client) -> bool:
        """用账号密码登录，登录态存在 client 的 cookie jar 里。"""
        s = self._settings
        if s["auth_mode"] != "password":
            return True
        if not s["login_path"]:
            logger.warning("OA 未配置登录路径，跳过登录")
            return False
        try:
            resp = client.post(
                f"{s['base_url'].rstrip('/')}/{s['login_path'].lstrip('/')}",
                data={"loginId": s["username"], "password": s["password"], "login_type": "1"},
                timeout=10,
            )
        except httpx.HTTPError as exc:
            logger.warning("OA 登录失败：%s", exc)
            return False
        if resp.status_code >= 400:
            logger.warning("OA 登录返回 HTTP %s", resp.status_code)
            return False
        return True

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
                resp = client.get(
                    f"{s['base_url'].rstrip('/')}/{s['todo_path'].lstrip('/')}",
                    headers=headers,
                    params=_parse_query(s["todo_query_params"]),
                    timeout=float(s["timeout_seconds"]),
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
