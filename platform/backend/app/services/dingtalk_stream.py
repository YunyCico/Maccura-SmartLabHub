"""钉钉 Stream 模式客户端：自动记录"每条日程的创建人"。

为什么需要它
------------
钉钉开放接口**不返回每条日程的创建人**：日程详情里只有 `organizer`（组织者）
和 `createTime`；钉钉界面上的「动态：XX 新建了日程」没有任何查询接口能取
（activities / operationLogs 全 404，订阅日历的 participants 接口
PERMISSION_DENIED，旧版 oapi 日程接口只有写接口没有查询接口）。

**唯一带创建人的地方是「日程变更」事件推送**（`calendar_event_change`），
载荷里的 `operator` 就是操作人，`calendarEventId` 正好等于日程接口返回的
日程 id，可以精确关联。

为什么用 Stream 模式
-------------------
钉钉事件推送有三种：HTTP 回调（要公网地址）、SyncHTTP（要 RDS）、Stream
（WebSocket 长连接，**不需要公网地址**）。本地/内网部署只有 Stream 可用。

协议要点（对齐官方 dingtalk-stream-sdk-python，已实测本机可连通）：
1. ``POST /v1.0/gateway/connections/open``，**clientId / clientSecret 放在
   body 里**（不是 header！放 header 会返回 400「系统错误」），带上
   ``subscriptions=[{"type": "EVENT", "topic": "*"}]``；
2. ``websockets.connect(f"{endpoint}?ticket={quote_plus(ticket)}")``；
3. 收到的消息是**纯 JSON 文本帧**：
   ``{"type": "EVENT", "headers": {..., "eventType": "calendar_event_change"}, "data": {...}}``；
4. 处理完回一个 ack 帧。

降级：任何一步失败都不影响主应用，只是采集不到创建人；后台线程自动重连。
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading
import time
from typing import Any
from urllib.parse import quote_plus

import httpx
import websockets

from app.core.database import record_event_creator
from app.services import integration_config

logger = logging.getLogger(__name__)

OPEN_CONNECTION_API = "https://api.dingtalk.com/v1.0/gateway/connections/open"
CALENDAR_EVENT_CHANGE = "calendar_event_change"

# 采集状态，供设置页/首页展示
_STATUS: dict[str, Any] = {
    "running": False,
    "connected": False,
    "last_error": "",
    "last_event_at": "",
    "recorded_count": 0,
    "last_connect_at": "",
}


def status() -> dict[str, Any]:
    """采集状态快照。"""
    return dict(_STATUS)


def _local_ip() -> str:
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


def _operator_fields(operator: Any) -> tuple[str, str, str]:
    """从推送的 operator 对象里抠出 (姓名, userid, unionId)。

    钉钉的 operator 结构在不同版本里字段名不完全一致，这里对常见几种
    都做兼容，拿到什么算什么。
    """
    if not isinstance(operator, dict):
        return "", "", ""
    name = ""
    for key in ("name", "displayName", "operatorName", "creatorName"):
        value = operator.get(key)
        if value:
            name = str(value).strip()
            break
    userid = ""
    for key in ("operatorUserId", "userId", "staffId", "operatorId"):
        value = operator.get(key)
        if value:
            userid = str(value).strip()
            break
    union_id = ""
    for key in ("operatorUnionId", "unionId", "operatorUnionid"):
        value = operator.get(key)
        if value:
            union_id = str(value).strip()
            break
    return name, userid, union_id


def handle_calendar_event(data: dict[str, Any]) -> bool:
    """处理一条日程变更事件，把创建人记进本地库。返回是否记录成功。"""
    if not isinstance(data, dict):
        return False
    event_id = str(data.get("calendarEventId") or "").strip()
    if not event_id:
        return False
    change_type = str(data.get("changeType") or "").strip()
    name, userid, union_id = _operator_fields(data.get("operator"))
    calendar_id = str(data.get("calendarId") or "").strip()
    event_time = str(data.get("calendarEventUpdateTime") or data.get("eventTime") or "").strip()
    try:
        record_event_creator(
            event_id,
            calendar_id=calendar_id,
            change_type=change_type,
            operator_name=name,
            operator_userid=userid,
            operator_union_id=union_id,
            event_time=event_time,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("记录日程创建人失败：%s", exc)
        return False
    _STATUS["recorded_count"] = int(_STATUS.get("recorded_count") or 0) + 1
    _STATUS["last_event_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    logger.info(
        "已记录日程创建人：event=%s change=%s operator=%s(%s/%s)",
        event_id, change_type, name or "-", userid or "-", union_id or "-",
    )
    return True


def _ack(message_id: str) -> dict[str, Any]:
    """回给钉钉的确认帧。"""
    return {
        "code": 200,
        "headers": {"messageId": message_id, "contentType": "application/json"},
        "messageId": message_id,
        "type": "SYSTEM",
    }


class DingTalkStreamListener:
    """后台线程跑着的长连接客户端。"""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ---------- 对外 ----------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        config = integration_config.effective_settings()["dingtalk"]
        if not (config.get("app_key") and config.get("app_secret")):
            logger.info("未配置钉钉凭据，跳过事件推送采集")
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="dingtalk-stream", daemon=True)
        self._thread.start()
        _STATUS["running"] = True

    def stop(self) -> None:
        self._stop.set()
        _STATUS["running"] = False
        _STATUS["connected"] = False

    # ---------- 内部 ----------

    def _run(self) -> None:
        backoff = 5
        while not self._stop.is_set():
            try:
                asyncio.run(self._session())
                backoff = 5
            except Exception as exc:  # noqa: BLE001
                _STATUS["connected"] = False
                _STATUS["last_error"] = str(exc)[:200]
                logger.warning("钉钉事件推送连接异常：%s", exc)
            if self._stop.is_set():
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)

    async def _session(self) -> None:
        config = integration_config.effective_settings()["dingtalk"]
        body = {
            "clientId": config.get("app_key") or "",
            "clientSecret": config.get("app_secret") or "",
            "subscriptions": [{"type": "EVENT", "topic": "*"}],
            "ua": "smartlabhub-dingtalk-stream/1.0",
            "localIp": _local_ip(),
        }
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                OPEN_CONNECTION_API,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                json=body,
            )
        if resp.status_code != 200:
            raise RuntimeError(f"打开 stream 连接失败 HTTP {resp.status_code}: {resp.text[:120]}")
        info = resp.json()
        endpoint = str(info.get("endpoint") or "")
        ticket = str(info.get("ticket") or "")
        if not endpoint or not ticket:
            raise RuntimeError(f"stream 连接返回异常：{resp.text[:120]}")
        uri = f"{endpoint}?ticket={quote_plus(ticket)}"
        logger.info("连接钉钉事件推送：%s", endpoint)
        _STATUS["last_connect_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _STATUS["last_error"] = ""
        async with websockets.connect(uri, open_timeout=20) as ws:
            _STATUS["connected"] = True
            logger.info("钉钉事件推送已连接")
            try:
                async for raw in ws:
                    await self._handle_raw(raw, ws)
            finally:
                _STATUS["connected"] = False

    async def _handle_raw(self, raw: Any, ws: Any) -> None:
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                return
        try:
            message = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            logger.warning("收到无法解析的推送帧：%r", str(raw)[:200])
            return
        if not isinstance(message, dict):
            return
        message_type = message.get("type")
        headers = message.get("headers") or {}
        message_id = str(headers.get("messageId") or "")

        if message_type == "EVENT":
            event_type = str(headers.get("eventType") or "")
            if event_type == CALENDAR_EVENT_CHANGE:
                try:
                    handle_calendar_event(message.get("data") or {})
                except Exception as exc:  # noqa: BLE001
                    logger.warning("处理日程变更事件失败：%s", exc)
        if message_id:
            # 必须回 ack，否则钉钉会认为消费失败并断开连接
            try:
                await ws.send(json.dumps(_ack(message_id)))
            except Exception as exc:  # noqa: BLE001
                logger.warning("回执发送失败：%s", exc)


_LISTENER: DingTalkStreamListener | None = None


def start_dingtalk_stream() -> None:
    """启动采集线程（幂等）。"""
    global _LISTENER
    if _LISTENER is None:
        _LISTENER = DingTalkStreamListener()
    _LISTENER.start()
