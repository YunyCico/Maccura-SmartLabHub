"""配置管理：从本地 config.json 读取，避免密钥硬编码在代码里。

按《对外数据拉取接口（文件下载版）》文档要求：
- 中台地址、登录账号 api_data、登录密码为**固定配置**，用户无感
- 页面需要用户填写的只有两项：username（数据权限用户名）与 X-Open-Api-Key
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
CONFIG_PATH = BASE_DIR / "config.json"

# 文档明确给定的固定配置，代码内置，用户无需填写
DOC_API_BASE = "http://10.19.51.251:8080"
DOC_PLATFORM_BASE = "http://10.19.51.251:5173"
DOC_LOGIN_USERNAME = "api_data"
DOC_LOGIN_PASSWORD = "M_bWFjY3VyYTEyMw=="  # 已编码，直接传

DEFAULT_CONFIG: dict[str, Any] = {
    "platformBase": DOC_PLATFORM_BASE,
    "apiBase": DOC_API_BASE,
    # 登录固定配置
    "loginUsername": DOC_LOGIN_USERNAME,
    "loginPassword": DOC_LOGIN_PASSWORD,
    # 用户侧配置
    "username": "",
    "openApiKey": "",
    "autoLogin": True,
    "pageSizeLimit": 10000,
    "requestTimeout": 300,
}

_lock = threading.Lock()


def _ensure_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict[str, Any]:
    with _lock:
        cfg = dict(DEFAULT_CONFIG)
        if CONFIG_PATH.exists():
            try:
                cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
            except Exception:
                pass
        # 环境变量优先级最高（便于 CI / 不落盘场景）
        env_key = os.environ.get("DFP_OPEN_API_KEY")
        if env_key:
            cfg["openApiKey"] = env_key
        env_user = os.environ.get("DFP_USERNAME")
        if env_user:
            cfg["username"] = env_user
        return cfg


def save_config(patch: dict[str, Any]) -> dict[str, Any]:
    """只允许写用户侧配置，固定配置不接受页面改写。"""
    user_writable = {"username", "openApiKey", "autoLogin",
                     "pageSizeLimit", "requestTimeout"}
    cfg = load_config()
    for k, v in patch.items():
        if k in user_writable:
            cfg[k] = v
    # 固定配置始终回落到文档值，防止被本地文件改坏
    cfg["apiBase"] = DOC_API_BASE
    cfg["platformBase"] = DOC_PLATFORM_BASE
    cfg["loginUsername"] = DOC_LOGIN_USERNAME
    cfg["loginPassword"] = DOC_LOGIN_PASSWORD
    with _lock:
        CONFIG_PATH.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return cfg


def mask_key(key: str) -> str:
    if not key or len(key) < 12:
        return ""
    return f"{key[:8]}****{key[-4:]}"