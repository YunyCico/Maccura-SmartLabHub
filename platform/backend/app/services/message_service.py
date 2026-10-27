"""消息聚合服务。

首页只调这一个函数，内部并行拉取钉钉 / Salesforce / OA，归一化、去重、排序。
设计要点：
- 任一家未配置或失败，只影响它自己那一条来源状态，绝不让整个首页报错
- 全部未配置时返回示例数据，首页不会是一片空白（示例条目 live=False 会标注）
- 带快照缓存 + 单飞保护：首页会定时轮询，多个标签页也可能同时来，
  没有缓存的话每次都真打一遍钉钉/Salesforce，API 配额扛不住。
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from app.services.dingtalk_messages import DingTalkAdapter
from app.services.message_models import (
    PRIORITY_HIGH,
    PRIORITY_LOW,
    PRIORITY_MEDIUM,
    SOURCE_DINGTALK,
    SOURCE_OA,
    SOURCE_SALESFORCE,
    MessageItem,
    dedupe_key,
)
from app.services.oa_messages import OaAdapter
from app.services.salesforce_messages import SalesforceAdapter

logger = logging.getLogger(__name__)

# 快照缓存：首页轮询间隔 60s，后端再留一点余量，让同一秒内的多个请求
# （多个标签页 + 首页 + 看板）共用一份结果。
_SNAPSHOT_TTL = 20.0
_snapshot_lock = threading.Lock()
_snapshot_at = 0.0
_snapshot_key = ""
_snapshot_data: dict[str, Any] = {}

_PRIORITY_ORDER = {PRIORITY_HIGH: 0, PRIORITY_MEDIUM: 1, PRIORITY_LOW: 2}

# 未配置来源的占位数据，live=False，前端会显示"示例数据"角标
# 按来源组织：某一家没配就只撤掉那一家，另外两家的示例照旧显示，
# 不能因为配了钉钉就把 OA / Salesforce 的示例也一起清掉（首页会直接空掉）。
_PLACEHOLDERS: dict[str, list[MessageItem]] = {
    SOURCE_DINGTALK: [
        MessageItem(
            source=SOURCE_DINGTALK,
            external_id="demo-dingtalk-1",
            title="确认本周实验室服务问题汇总",
            detail="实验室运营管理部 · 待你处理",
            due="今天 15:00 前",
            priority=PRIORITY_HIGH,
            category="待办",
            kind="todo",
            live=False,
        ),
    ],
    SOURCE_OA: [
        MessageItem(
            source=SOURCE_OA,
            external_id="demo-oa-1",
            title="审批智慧实验室月度报告",
            detail="OA 待办 · 发起人：实验室运营管理部",
            due="今天 18:00 前",
            priority=PRIORITY_HIGH,
            category="OA 待办",
            live=False,
        ),
    ],
    SOURCE_SALESFORCE: [
        MessageItem(
            source=SOURCE_SALESFORCE,
            external_id="demo-salesforce-1",
            title="跟进重点客户服务反馈",
            detail="Salesforce · 3 条待跟进",
            due="今天",
            priority=PRIORITY_MEDIUM,
            category="工单",
            live=False,
        ),
    ],
}


def collect_messages(user_id: str = "", limit: int = 50) -> dict[str, Any]:
    """取消息聚合结果（带快照缓存与单飞保护）。

    首页会定时轮询本接口，多个标签页也可能同时请求；直接每次真打一遍
    钉钉/Salesforce 会很快耗尽 API 配额。因此：
    - 同一 userid 且在 TTL 内 → 直接返回上次的快照
    - 并发首次请求 → 只让一个线程真去取数，其余等它的结果（单飞）
    """
    global _snapshot_at, _snapshot_data, _snapshot_key

    key = f"{user_id}|{limit}"
    now = time.monotonic()
    with _snapshot_lock:
        fresh = (
            _snapshot_data
            and _snapshot_key == key
            and (now - _snapshot_at) < _SNAPSHOT_TTL
        )
        if fresh:
            payload = dict(_snapshot_data)
            payload["cached"] = True
            payload["age"] = round(now - _snapshot_at, 1)
            return payload

    # 单飞：第一个进来的线程去取数，其他线程等它完成后直接用结果
    with _snapshot_lock:
        if (
            _snapshot_data
            and _snapshot_key == key
            and (time.monotonic() - _snapshot_at) < _SNAPSHOT_TTL
        ):
            payload = dict(_snapshot_data)
            payload["cached"] = True
            payload["age"] = round(time.monotonic() - _snapshot_at, 1)
            return payload

    data = _collect_messages_uncached(user_id, limit)
    with _snapshot_lock:
        _snapshot_at = time.monotonic()
        _snapshot_key = key
        _snapshot_data = data
    payload = dict(data)
    payload["cached"] = False
    payload["age"] = 0.0
    return payload


def invalidate_snapshot() -> None:
    """清掉快照缓存。配置变更（登录/断开/保存）后调用，下一次请求会真取数。"""
    global _snapshot_at, _snapshot_data, _snapshot_key
    with _snapshot_lock:
        _snapshot_data = {}
        _snapshot_at = 0.0
        _snapshot_key = ""


def _collect_messages_uncached(user_id: str, limit: int = 50) -> dict[str, Any]:
    """真去三家取数，归一化、去重、排序。"""
    dingtalk = DingTalkAdapter()
    oa = OaAdapter()
    salesforce = SalesforceAdapter()

    # 前端只在钉钉容器内才能免登拿到 userid；本地/无 CorpId 时前端传的是空串，
    # 这时要回落到设置页里配的「默认 userid」，否则那个字段形同虚设。
    effective_user_id = (user_id or "").strip() or dingtalk.configured_user_id()

    def fetch_dingtalk() -> list[MessageItem]:
        return dingtalk.fetch(effective_user_id)

    def fetch_oa() -> list[MessageItem]:
        # 只探活：OA 的待办明细在「OA 业务看板」里
        oa.fetch()
        return []

    def fetch_salesforce() -> list[MessageItem]:
        # 只探活：不取任何业务明细，明细在「Salesforce 业务看板」里。
        # 复用板块的共享缓存——看板那边刚取过就不要再打一次上游。
        from app.services.salesforce_messages import fetch_boards_cached

        boards = fetch_boards_cached()
        appr = boards.get("approvals") or {}
        if appr.get("error"):
            salesforce._error = str(appr["error"])
        elif appr.get("connected"):
            salesforce._error = ""
        return []

    results: dict[str, list[MessageItem]] = {}
    errors: dict[str, str] = {}
    jobs = {
        "dingtalk": fetch_dingtalk,
        "oa": fetch_oa,
        "salesforce": fetch_salesforce,
    }
    adapters = {
        "dingtalk": dingtalk,
        "oa": oa,
        "salesforce": salesforce,
    }
    adapters = {
        "dingtalk": dingtalk,
        "oa": oa,
        "salesforce": salesforce,
    }
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {pool.submit(fn): name for name, fn in jobs.items()}
        for future, name in futures.items():
            try:
                results[name] = future.result() or []
            except Exception as exc:
                logger.warning("%s 消息拉取失败：%s", name, exc)
                results[name] = []
                errors[name] = str(exc)
                # ★ 同步回适配器自己的 _error：
                #   _source_status 会用 adapter.status() 覆盖 status["error"]，
                #   不写回去的话首页角标就看不到"这一家挂了"，只显示"未配置"。
                adapter = adapters.get(name)
                if adapter is not None and hasattr(adapter, "_error"):
                    adapter._error = str(exc)
                # ★ 同步回适配器自己的 _error：
                #   _source_status 会用 adapter.status() 覆盖 status["error"]，
                #   不写回去的话首页角标就看不到"这一家挂了"，只显示"未配置"。
                adapter = adapters.get(name)
                if adapter is not None and hasattr(adapter, "_error"):
                    adapter._error = str(exc)

    merged = [item for group in results.values() for item in group]
    configured = {
        SOURCE_DINGTALK: dingtalk.configured(),
        SOURCE_OA: oa.configured(),
        SOURCE_SALESFORCE: salesforce.configured(),
    }

    # 按来源补占位数据：某一家没配，就只用它自己的示例顶上。
    # 之前是「只要有一家配了就全撤」，导致配好钉钉后首页直接空掉，
    # 看起来就像"保存后原来的东西不见了"。
    # Salesforce / OA 不补示例：它们在消息中心只显示连接状态，明细在各自业务看板。
    placeholder_used = False
    for source, is_configured in configured.items():
        if is_configured or source in (SOURCE_SALESFORCE, SOURCE_OA):
            continue
        placeholder_used = True
        merged.extend(_PLACEHOLDERS.get(source, []))

    deduped = _dedupe(merged)
    ordered = _sort(deduped)[: max(1, limit)]

    sources = [
        _source_status("dingtalk", dingtalk.configured(), len(results["dingtalk"]), errors.get("dingtalk"), dingtalk),
        _source_status("oa", oa.configured(), len(results["oa"]), errors.get("oa"), oa),
        _source_status("salesforce", salesforce.configured(), len(results["salesforce"]), errors.get("salesforce"), salesforce),
    ]

    return {
        "items": [item.normalized() for item in ordered],
        "sources": sources,
        "using_placeholder": placeholder_used,
        # 本次取数的时刻（UTC ISO），前端用它显示"上次更新 hh:mm:ss"
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def _dedupe(items: list[MessageItem]) -> list[MessageItem]:
    seen: set[str] = set()
    unique: list[MessageItem] = []
    for item in items:
        key = dedupe_key(item)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _sort(items: list[MessageItem]) -> list[MessageItem]:
    """高优先级在前，同优先级保持各家原始顺序（未完成在前）。"""
    indexed = list(enumerate(items))
    indexed.sort(key=lambda pair: (_PRIORITY_ORDER.get(pair[1].priority, 1), pair[0]))
    return [item for _, item in indexed]


def _source_status(name: str, enabled: bool, count: int, error: str | None, adapter: Any) -> dict[str, Any]:
    status: dict[str, Any] = {
        "source": name,
        "enabled": enabled,
        "count": count,
        "error": error or "",
    }
    if hasattr(adapter, "status"):
        status.update(adapter.status())
    return status


def source_state() -> list[dict[str, Any]]:
    """只返回配置状态，不拉数据。用于前端显示"已连接/未配置"角标。"""
    return [
        _source_status("dingtalk", DingTalkAdapter().configured(), 0, None, DingTalkAdapter()),
        _source_status("oa", OaAdapter().configured(), 0, None, OaAdapter()),
        _source_status("salesforce", SalesforceAdapter().configured(), 0, None, SalesforceAdapter()),
    ]
