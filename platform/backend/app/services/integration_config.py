"""消息互通配置读写。

配置存在 platform/backend/data/integrations.json，与 .env 分离：
- .env 继续放数据库等基础设施配置
- 三家消息接口的凭据与路径放这里，可通过界面修改，无需重启后端

密码类字段永远不完整回传给前端，只返回「已配置 / 未配置」。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.core.config import get_settings

logger = logging.getLogger(__name__)

DATA_FILE = Path(__file__).resolve().parents[3] / "data" / "integrations.json"

# 允许前端写入的字段白名单，防止任意键写入
ALLOWED_FIELDS = {
    "dingtalk": [
        "corp_id",
        "app_key",
        "app_secret",
        "message_kinds",
        "user_id",
        "trip_creator_map",
    ],
    "oa": [
        "base_url",
        "auth_mode",
        "username",
        "password",
        "token",
        "auth_header",
        "login_path",
        "todo_path",
        "todo_query_params",
        "rows_path",
        "field_map",
        "default_title",
        "timeout_seconds",
        "max_items",
    ],
    "salesforce": [
        "auth_mode",
        "instance_url",
        "api_version",
        "org_id",
        "username",
        "password",
        "totp_secret",
        "access_token",
        "client_id",
        "client_secret",
        "refresh_token",
        "token",
        "task_soql",
        "event_soql",
        "timeout_seconds",
        "max_items",
        "window_days",
    ],
}

# 回传时脱敏的字段
SECRET_FIELDS = {
    "app_secret",
    "password",
    "token",
    "access_token",
    "client_secret",
    "totp_secret",
    "refresh_token",
}

DEFAULT_CONFIG: dict[str, dict[str, Any]] = {
    "dingtalk": {
        "corp_id": "",
        "app_key": "",
        "app_secret": "",
        "message_kinds": "todo,calendar,trip",
        "user_id": "",
        # 行程创建人对照表，每行「行程标题=创建人」。钉钉接口不返回每条日程的
        # 创建人，自动识别只能覆盖一部分，剩下的在这里指定（见 dingtalk_messages）。
        "trip_creator_map": "",
    },
    "oa": {
        "base_url": "",
        "auth_mode": "password",
        "username": "",
        "password": "",
        "token": "",
        "auth_header": "",
        "login_path": "",
        "todo_path": "",
        "todo_query_params": "",
        "rows_path": "",
        "field_map": "",
        "default_title": "OA 待办",
        # parser：json = 通用 JSON 接口（默认）；weaver_xml = 泛微 e-cology 表格 XML
        "parser": "json",
        "xml_path": "",
        "page_id": "",
        "timeout_seconds": 10,
        "max_items": 30,
    },
    "salesforce": {
        "auth_mode": "totp",
        "instance_url": "https://maccuracn.my.sfcrmproducts.cn",
        "api_version": "v62.0",
        "org_id": "00DC8000009rygr",
        "username": "",
        "password": "",
        "totp_secret": "",
        "access_token": "",
        "client_id": "",
        "client_secret": "",
        "refresh_token": "",
        "token": "",
        "task_soql": "",
        "event_soql": "",
        "timeout_seconds": 20,
        "max_items": 30,
        "window_days": 90,
    },
}


class IntegrationUpdate(BaseModel):
    """三家配置的部分更新；未传的字段保持原值。"""

    dingtalk: dict[str, Any] = Field(default_factory=dict)
    oa: dict[str, Any] = Field(default_factory=dict)
    salesforce: dict[str, Any] = Field(default_factory=dict)


def load_config() -> dict[str, Any]:
    """读配置文件；不存在或损坏时回落到默认结构，绝不抛异常。"""
    merged = {key: dict(value) for key, value in DEFAULT_CONFIG.items()}
    try:
        raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return merged
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("集成配置读取失败，使用默认值：%s", exc)
        return merged
    if isinstance(raw, dict):
        for section, values in raw.items():
            if section in merged and isinstance(values, dict):
                merged[section].update(values)
    return merged


def save_config(
    updates: dict[str, Any],
    clear_secrets: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """原子写入：先写临时文件再 replace，避免写一半损坏配置。

    clear_secrets：显式清空某些密码类字段（如登录时用户未勾选"记住"，
    后端也不落盘账号密码）。会绕过「空字符串=不修改」的保护，仅供明确要清空的场景。
    """
    current = load_config()
    for section, values in updates.items():
        if section not in current or not isinstance(values, dict):
            continue
        for key, value in values.items():
            if key in ALLOWED_FIELDS.get(section, []):
                # 前端传空字符串给密码类字段时，视为"不修改"，
                # 避免脱敏后回传的空值把已存密码擦掉
                if key in SECRET_FIELDS and value == "" and current[section].get(key):
                    continue
                current[section][key] = value

    # 显式清空：绕过上面的空值保护，真正把字段抹掉
    if clear_secrets:
        for section, keys in clear_secrets.items():
            if section not in current or not isinstance(keys, list):
                continue
            for key in keys:
                if key in ALLOWED_FIELDS.get(section, []):
                    current[section][key] = ""

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(DATA_FILE.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(current, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_path, DATA_FILE)
    except OSError as exc:
        logger.error("集成配置写入失败：%s", exc)
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    return current


def secret_hint(value: str) -> str:
    """
    密码类字段的脱敏提示：只暴露长度和末几位，让前端能显示"确实存着"，
    又不泄露明文。例：长度 64、末 4 位 HcAk -> "64 位，末 4 位 HcAk"。
    """
    text = str(value or "")
    if not text:
        return ""
    tail = text[-4:] if len(text) > 8 else ""
    if tail:
        return f"{len(text)} 位，末 4 位 {tail}"
    return f"{len(text)} 位"


def public_view(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """给前端看的版本：密码类字段只回传是否已配置 + 脱敏提示。"""
    config = config or load_config()
    result: dict[str, Any] = {}
    for section, values in config.items():
        safe: dict[str, Any] = {}
        for key, value in values.items():
            if key in SECRET_FIELDS:
                safe[f"{key}_configured"] = bool(value)
                safe[f"{key}_hint"] = secret_hint(value)
                safe[key] = ""
            else:
                safe[key] = value
        result[section] = safe
    return result


def effective_settings() -> dict[str, Any]:
    """
    文件配置 + .env 的合并结果，供运行时读取。

    优先级：integrations.json > .env。.env 仍作为初始凭据来源，
    一旦在界面里保存过，就以文件为准。
    """
    config = load_config()
    settings = get_settings()
    dingtalk = config["dingtalk"]
    oa = config["oa"]
    salesforce = config["salesforce"]
    return {
        "dingtalk": {
            "corp_id": dingtalk.get("corp_id") or settings.dingtalk_corp_id,
            "app_key": dingtalk.get("app_key") or settings.dingtalk_app_key,
            "app_secret": dingtalk.get("app_secret") or settings.dingtalk_app_secret,
            "message_kinds": dingtalk.get("message_kinds") or settings.dingtalk_message_kinds,
            "user_id": dingtalk.get("user_id") or settings.dingtalk_user_id,
            # 部门行程创建人对照表。每行「行程标题=创建人」，用 \n 分隔。
            # 钉钉开放接口不返回每条日程的创建人（见 dingtalk_messages._trip_creator），
            # 自动识别只能覆盖一部分，剩下的在这里指定。
            "trip_creator_map": dingtalk.get("trip_creator_map") or "",
        },
        "oa": {
            "base_url": oa.get("base_url") or settings.oa_base_url,
            "auth_mode": oa.get("auth_mode") or settings.oa_auth_mode,
            "username": oa.get("username") or settings.oa_username,
            "password": oa.get("password") or settings.oa_password,
            "token": oa.get("token") or settings.oa_token,
            "auth_header": oa.get("auth_header") or settings.oa_auth_header,
            "login_path": oa.get("login_path") or settings.oa_login_path,
            "todo_path": oa.get("todo_path") or settings.oa_todo_path,
            "todo_query_params": oa.get("todo_query_params") or settings.oa_todo_query_params,
            "rows_path": oa.get("rows_path") or settings.oa_rows_path,
            "field_map": oa.get("field_map") or settings.oa_field_map,
            "default_title": oa.get("default_title") or settings.oa_default_title,
            # 泛微表格 XML 专用：parser=weaver_xml 时才用得上
            "parser": oa.get("parser") or "json",
            "xml_path": oa.get("xml_path") or "",
            "page_id": oa.get("page_id") or "",
            "timeout_seconds": oa.get("timeout_seconds") or settings.oa_timeout_seconds,
            "max_items": oa.get("max_items") or settings.oa_max_items,
        },
        "salesforce": {
            "auth_mode": salesforce.get("auth_mode") or settings.salesforce_auth_mode,
            "instance_url": salesforce.get("instance_url") or settings.salesforce_instance_url,
            "api_version": salesforce.get("api_version") or settings.salesforce_api_version,
            "org_id": salesforce.get("org_id") or settings.salesforce_org_id,
            "username": salesforce.get("username") or settings.salesforce_username,
            "password": salesforce.get("password") or settings.salesforce_password,
            "totp_secret": salesforce.get("totp_secret") or settings.salesforce_totp_secret,
            "access_token": salesforce.get("access_token") or settings.salesforce_access_token,
            "client_id": salesforce.get("client_id") or settings.salesforce_client_id,
            "client_secret": salesforce.get("client_secret") or settings.salesforce_client_secret,
            "refresh_token": salesforce.get("refresh_token") or settings.salesforce_refresh_token,
            "token": salesforce.get("token") or settings.salesforce_token,
            "task_soql": salesforce.get("task_soql") or "",
            "event_soql": salesforce.get("event_soql") or "",
            "timeout_seconds": salesforce.get("timeout_seconds") or settings.salesforce_timeout_seconds,
            "max_items": salesforce.get("max_items") or settings.salesforce_max_items,
            "window_days": salesforce.get("window_days") or settings.salesforce_window_days,
        },
    }