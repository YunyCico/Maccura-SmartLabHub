"""数据中台开放接口客户端。

严格按《对外数据拉取接口（文件下载版）》实现：
1. 用统一对外账号 api_data 调 /api/auth/login 取 JWT token
2. 调 /api/open/data/pullFile，请求头同时带 Authorization 与 X-Open-Api-Key
3. 从 data.downloadUrl 下载 ZIP（无需认证头）

已实测（2026-10-19）确认的要点：
- 只带 X-Open-Api-Key → 401「未认证或token无效，请先登录」
- 只带 Authorization → 401「缺少对外接口 key」
- 登录响应 data.username 字段存在（api_data）；token 有效期约 24h
- 下载链接的文件服务端 3 分钟后自动清理，必须拿到后立即下载
"""
from __future__ import annotations

import json
import threading
import time
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx

PULL_FILE_PATH = "/api/open/data/pullFile"
LOGIN_PATH = "/api/auth/login"
PASS_RATE_PATH = "/api/open/data/supplier-pass-rate"
DELIVERY_RATE_PATH = "/api/open/data/supplier-delivery-rate"

# 服务端 ZIP 保留时长（文档：3 分钟）
ZIP_TTL_SECONDS = 180


class PlatformError(Exception):
    def __init__(self, message: str, code: int | None = None, detail: Any = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.detail = detail


class DataPlatformClient:
    """单次调用的轻量客户端，不持有跨请求状态。"""

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.api_base = str(cfg.get("apiBase", "")).rstrip("/")
        self.platform_base = str(cfg.get("platformBase", "")).rstrip("/")
        self.login_username = str(cfg.get("loginUsername") or "api_data")
        self.login_password = str(cfg.get("loginPassword") or "")
        self.username = str(cfg.get("username") or "").strip()
        self.open_api_key = str(cfg.get("openApiKey") or "").strip()
        timeout = float(cfg.get("requestTimeout") or 300)
        # trust_env=False：数据中台在内网（10.19.51.251），若继承系统 HTTP_PROXY
        # 环境变量会把请求转发到外部代理并被拒绝（ConnectionRefused / os error 10061）。
        self._client = httpx.Client(timeout=timeout, follow_redirects=True, trust_env=False)

    # ---------- 登录 ----------

    def login(self) -> dict[str, Any]:
        if not self.api_base:
            raise PlatformError("未配置数据中台 API 地址")
        if not self.login_username or not self.login_password:
            raise PlatformError("未配置统一对外登录账号")
        payload = {
            "username": self.login_username,
            "password": self.login_password,
            "rememberMe": False,
        }
        r = self._client.post(f"{self.api_base}{LOGIN_PATH}", json=payload)
        if r.status_code != 200:
            raise PlatformError(f"登录 HTTP {r.status_code}", detail=r.text[:400])
        body = r.json()
        if body.get("code") != 200:
            raise PlatformError(body.get("message") or "登录失败", code=body.get("code"))
        data = body.get("data") or {}
        if not data.get("token"):
            raise PlatformError("登录响应中没有 token")
        return data

    def _require_user_config(self) -> None:
        if not self.username:
            raise PlatformError("请先在页面填写 username（数据权限用户名）")
        if not self.open_api_key:
            raise PlatformError("请先在页面填写 X-Open-Api-Key（对外接口 Key）")

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "X-Open-Api-Key": self.open_api_key,
            "Content-Type": "application/json",
        }

    # ---------- 数据拉取 ----------

    def pull_file(self, body: dict[str, Any], token: str | None = None) -> dict[str, Any]:
        """调用 pullFile 获取下载链接。"""
        self._require_user_config()
        token = token or self.login()["token"]
        r = self._client.post(
            f"{self.api_base}{PULL_FILE_PATH}",
            headers=self._headers(token),
            json=body,
        )
        if r.status_code != 200:
            raise PlatformError(f"HTTP {r.status_code}", detail=r.text[:400])
        j = r.json()
        if j.get("code") != 200:
            raise PlatformError(j.get("message") or "拉取失败", code=j.get("code"))
        return j.get("data") or {}

    def download_zip(self, url: str) -> bytes:
        """按 downloadUrl 下载 ZIP。该请求无需认证头。"""
        if url.startswith("/"):
            url = self.platform_base + url
        r = self._client.get(url)
        if r.status_code != 200:
            raise PlatformError(
                f"下载 ZIP 失败 HTTP {r.status_code}"
                "（若刚生成，可能是已超过 3 分钟被服务端清理，请重新拉取）"
            )
        return r.content

    # ---------- 供应商指标类接口 ----------

    def supplier_pass_rate(self, body: dict[str, Any], token: str | None = None) -> dict[str, Any]:
        self._require_user_config()
        token = token or self.login()["token"]
        r = self._client.post(
            f"{self.api_base}{PASS_RATE_PATH}",
            headers=self._headers(token),
            json=body,
        )
        return self._handle_json(r)

    def supplier_delivery_rate(
        self, body: dict[str, Any], token: str | None = None
    ) -> dict[str, Any]:
        self._require_user_config()
        token = token or self.login()["token"]
        r = self._client.post(
            f"{self.api_base}{DELIVERY_RATE_PATH}",
            headers=self._headers(token),
            json=body,
        )
        return self._handle_json(r)

    @staticmethod
    def _handle_json(r: httpx.Response) -> dict[str, Any]:
        if r.status_code != 200:
            raise PlatformError(f"HTTP {r.status_code}", detail=r.text[:400])
        j = r.json()
        if j.get("code") != 200:
            raise PlatformError(j.get("message") or "请求失败", code=j.get("code"))
        return j.get("data") or {}

    # ---------- 平台内辅助查询（给页面下拉选项用） ----------

    def _auth_get(self, path: str, token: str, params: dict | None = None) -> Any:
        r = self._client.get(
            f"{self.api_base}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params or {},
        )
        if r.status_code != 200:
            raise PlatformError(f"HTTP {r.status_code} 查询 {path}")
        body = r.json()
        if body.get("code") != 200:
            raise PlatformError(body.get("message") or f"查询 {path} 失败")
        return body.get("data")

    def list_datamap_domains(self, token: str) -> Any:
        return self._auth_get("/api/datamap/domains", token)

    def list_published_tables(self, token: str) -> Any:
        """已发布表（按当前 token 用户过滤，api_data 会返回空）。"""
        return self._auth_get("/api/datamap/published", token)

    def list_all_tables(self, token: str) -> Any:
        """全部表名（不按用户过滤），用作表名候选的兜底来源。"""
        return self._auth_get("/api/datamap/all-tables", token)

    def list_metric_domains(self, token: str) -> Any:
        return self._auth_get("/api/metric-domains", token)

    def list_published_metrics(self, token: str) -> Any:
        return self._auth_get("/api/metrics/published-list", token)

    def close(self) -> None:
        self._client.close()


# ---------------- Token 缓存（文档要求：Token 可缓存复用，过期后自动重登） ----------------

class TokenCache:
    """进程内缓存 JWT，带安全提前量，过期自动重新登录。"""

    # JWT exp 通常比实际可用时间略长，提前 5 分钟重登，避免边界失败
    SKEW_SECONDS = 300

    def __init__(self) -> None:
        self._token: str | None = None
        self._expire_at: float = 0.0
        self._lock = threading.Lock()

    def get(self, cfg: dict[str, Any]) -> tuple[str, bool]:
        """返回 (token, 是否新登录)。"""
        with self._lock:
            now = time.time()
            if self._token and now < self._expire_at:
                return self._token, False
            client = DataPlatformClient(cfg)
            try:
                data = client.login()
            finally:
                client.close()
            token = data["token"]
            self._token = token
            self._expire_at = self._decode_exp(token) or (now + 3600)
            return token, True

    @staticmethod
    def _decode_exp(token: str) -> float | None:
        """从 JWT payload 里读 exp，解不出就返回 None。"""
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            import base64

            claims = json.loads(base64.urlsafe_b64decode(payload))
            exp = claims.get("exp")
            return float(exp) - TokenCache.SKEW_SECONDS if exp else None
        except Exception:
            return None

    def clear(self) -> None:
        with self._lock:
            self._token = None
            self._expire_at = 0.0


TOKEN_CACHE = TokenCache()


# ---------------- ZIP 辅助 ----------------

def parse_manifest(zip_bytes: bytes) -> dict[str, Any]:
    empty: dict[str, Any] = {
        "tables": [], "metrics": [], "combinations": [], "message": ""
    }
    try:
        with zipfile.ZipFile(BytesIO(zip_bytes)) as z:
            if "manifest.json" not in z.namelist():
                return empty
            return json.loads(z.read("manifest.json").decode("utf-8"))
    except Exception:
        return empty


def list_zip_entries(zip_bytes: bytes) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with zipfile.ZipFile(BytesIO(zip_bytes)) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            out.append(
                {"name": info.filename, "size": info.file_size,
                 "kind": _classify(info.filename)}
            )
    return out


def _classify(name: str) -> str:
    if name == "manifest.json":
        return "manifest"
    if name.endswith(".columns.json"):
        return "columns"
    if name.startswith("tables/"):
        return "table"
    if name.startswith("metrics/"):
        return "metric"
    if name.startswith("combinations/"):
        return "combination"
    if name.endswith(".csv"):
        return "table"
    return "other"


def extract_zip(zip_bytes: bytes, dest: Path) -> list[str]:
    dest.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    root = dest.resolve()
    with zipfile.ZipFile(BytesIO(zip_bytes)) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            target = (dest / info.filename).resolve()
            if not str(target).startswith(str(root)):
                continue
            z.extract(info, dest)
            written.append(info.filename)
    return written


def now_stamp() -> str:
    return time.strftime("%Y%m%d%H%M%S")