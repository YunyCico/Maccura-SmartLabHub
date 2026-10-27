"""统一消息模型。

钉钉 / Salesforce / OA 三家接口结构各异，适配器各自把原始数据归一化成
MessageItem，首页只认这一个模型。字段一律用可选值 + 默认值，保证任何一家
接口缺字段都不会让聚合接口崩掉。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

SOURCE_DINGTALK = "dingtalk"
SOURCE_SALESFORCE = "salesforce"
SOURCE_OA = "oa"

SOURCE_LABELS = {
    SOURCE_DINGTALK: "钉钉",
    SOURCE_SALESFORCE: "Salesforce",
    SOURCE_OA: "公司 OA",
}

SOURCE_TONES = {
    SOURCE_DINGTALK: "blue",
    SOURCE_SALESFORCE: "orange",
    SOURCE_OA: "green",
}

PRIORITY_HIGH = "高"
PRIORITY_MEDIUM = "中"
PRIORITY_LOW = "低"

PRIORITIES = (PRIORITY_HIGH, PRIORITY_MEDIUM, PRIORITY_LOW)


@dataclass
class MessageItem:
    """一条归一化后的工作消息。"""

    source: str
    external_id: str
    title: str
    detail: str = ""
    due: str = ""
    priority: str = PRIORITY_MEDIUM
    url: str = ""
    category: str = ""
    # 消息类型键。钉钉四类来源（todo/notify/ding/calendar）用它归类，
    # 首页的"Ding DIng"分区要按它分子板块展示。
    kind: str = ""
    creator: str = ""
    received_at: str = ""
    # 适配器是否拿到了真实数据。False 表示这是占位/降级数据，首页会标注出来。
    live: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    def normalized(self) -> dict[str, Any]:
        priority = self.priority if self.priority in PRIORITIES else PRIORITY_MEDIUM
        source = self.source if self.source in SOURCE_LABELS else SOURCE_OA
        data = asdict(self)
        data.update(
            source=source,
            source_label=SOURCE_LABELS[source],
            tone=SOURCE_TONES[source],
            priority=priority,
            title=(self.title or "（无标题）").strip(),
            detail=(self.detail or "").strip(),
            due=(self.due or "").strip(),
        )
        return data


def normalize_priority(value: Any) -> str:
    """把各家五花八门的优先级表示折成 高/中/低。"""
    if isinstance(value, bool):
        return PRIORITY_HIGH if value else PRIORITY_LOW
    if isinstance(value, (int, float)):
        # 1 通常代表最高优先级（钉钉待办就是从 1 开始）
        if value <= 1:
            return PRIORITY_HIGH
        if value <= 3:
            return PRIORITY_MEDIUM
        return PRIORITY_LOW
    text = str(value or "").strip().lower()
    if not text:
        return PRIORITY_MEDIUM
    if text in {"1", "high", "urgent", "p1", "严重", "紧急", "高"}:
        return PRIORITY_HIGH
    if text in {"2", "3", "middle", "medium", "normal", "p2", "中"}:
        return PRIORITY_MEDIUM
    if text in {"4", "5", "low", "p3", "低"}:
        return PRIORITY_LOW
    return PRIORITY_MEDIUM


def format_due(value: Any, *, all_day: bool = False) -> str:
    """把各家的时间表示统一成首页能直接显示的短文本。

    all_day=True 时不显示钟点，只显示日期（钉钉全天日程只给 `date`，
    硬解析会补一个默认钟点，显示成「今天 09:00」是假的）。
    """
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value).strip()
        if not text:
            return ""
        moment = parse_datetime(text)
        if moment is None:
            # 不是时间格式就原样展示（如"今天 15:00 前"这类已加工文案）
            return text[:40]
    today = datetime.now(timezone.utc).astimezone()
    # 带时区的时间先换算成本机时区再比较与显示。
    # Salesforce 的 Event 存的是 UTC（"2026-10-22T09:30:00.000+0000"），
    # 不换算会把 09:30(UTC) 当成本地 09:30 显示，实际是北京 17:30。
    if moment.tzinfo is not None:
        moment = moment.astimezone()
    days = (moment.date() - today.date()).days
    if all_day:
        if days == 0:
            return "今天 全天"
        if days == 1:
            return "明天 全天"
        if days == -1:
            return "昨天 全天"
        if 0 < days <= 7:
            return f"{moment.strftime('%m-%d')} 全天"
        return moment.strftime("%Y-%m-%d")
    clock = moment.strftime("%H:%M")
    if days == 0:
        return f"今天 {clock}"
    if days == 1:
        return f"明天 {clock}"
    if days == -1:
        return f"昨天 {clock}"
    if 0 < days <= 7:
        return f"{moment.strftime('%m-%d')} {clock}"
    return moment.strftime("%Y-%m-%d")


def parse_datetime(value: str) -> datetime | None:
    """尽力解析时间字符串，解析不了返回 None 而不是抛异常。"""
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
        return moment if moment.tzinfo else moment.astimezone()
    except ValueError:
        pass
    # 毫秒时间戳（13 位）
    if text.isdigit():
        try:
            return datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc).astimezone()
        except (OverflowError, OSError, ValueError):
            return None
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).astimezone()
        except ValueError:
            continue
    return None


def dedupe_key(item: MessageItem) -> str:
    """跨来源去重键：同标题同到期时间视为同一条（例如 OA 待办同步到了钉钉）。"""
    title = item.title.strip().lower()
    return f"{item.source}:{item.external_id}" if item.external_id else f"{item.source}:{title}:{item.due}"
