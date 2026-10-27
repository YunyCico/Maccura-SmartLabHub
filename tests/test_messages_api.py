"""消息聚合层测试。

覆盖：归一化模型、优先级/时间折算、OA 路径解析、聚合降级行为。
不依赖任何真实外部接口，全部用桩数据。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI

BACKEND = Path(__file__).resolve().parents[1] / "platform" / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.services import message_service  # noqa: E402
from app.services.message_models import (  # noqa: E402
    PRIORITY_HIGH,
    PRIORITY_LOW,
    PRIORITY_MEDIUM,
    SOURCE_OA,
    MessageItem,
    format_due,
    normalize_priority,
)
from app.services.oa_messages import dig, _parse_json, _pick  # noqa: E402


def test_normalize_priority_covers_common_forms() -> None:
    assert normalize_priority("1") == PRIORITY_HIGH
    assert normalize_priority(1) == PRIORITY_HIGH
    assert normalize_priority("urgent") == PRIORITY_HIGH
    assert normalize_priority(True) == PRIORITY_HIGH
    assert normalize_priority(2) == PRIORITY_MEDIUM
    assert normalize_priority("normal") == PRIORITY_MEDIUM
    assert normalize_priority(5) == PRIORITY_LOW
    assert normalize_priority("低") == PRIORITY_LOW
    assert normalize_priority("") == PRIORITY_MEDIUM
    assert normalize_priority("看不懂的值") == PRIORITY_MEDIUM


def test_format_due_relative_labels() -> None:
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc).astimezone()
    # 注意别写死 "+2 小时"：晚上 22 点跑这个测试会跨到明天，必然挂。
    # 取当天正午，保证无论几点跑都在今天之内。
    noon = now.replace(hour=12, minute=0, second=0, microsecond=0)
    assert format_due(noon).startswith("今天")
    assert format_due(now + timedelta(days=1)).startswith("明天")
    assert format_due(now - timedelta(days=1)).startswith("昨天")
    assert format_due("") == ""
    # 无法解析的时间原样保留，不应抛异常
    assert format_due("今天 15:00 前") == "今天 15:00 前"


def test_format_due_accepts_epoch_millis() -> None:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).astimezone()
    stamp = int(now.replace(hour=12, minute=0, second=0, microsecond=0).timestamp() * 1000)
    assert format_due(stamp).startswith("今天")


def test_dig_supports_dot_and_index_paths() -> None:
    data = {"result": {"todos": [{"subject": "A"}, {"subject": "B"}]}}
    assert dig(data, "result.todos[1].subject") == "B"
    assert dig(data, "result.todos[5]") is None
    assert dig(data, "result.missing") is None
    assert dig(data, "") is data


def test_pick_falls_back_to_default() -> None:
    row = {"title": "审批"}
    assert _pick(row, "title") == "审批"
    assert _pick(row, "missing", "兜底") == "兜底"
    assert _pick(row, "", "兜底") == "兜底"


def test_parse_json_rejects_html() -> None:
    assert _parse_json('{"a":1}') == {"a": 1}
    assert _parse_json("<html>未登录</html>") is None
    assert _parse_json("") is None


def test_collect_messages_falls_back_to_placeholders(monkeypatch) -> None:
    """三家都未配置时，返回示例数据且带 using_placeholder 标记。"""
    monkeypatch.setattr(message_service.DingTalkAdapter, "configured", lambda self: False)
    monkeypatch.setattr(message_service.OaAdapter, "configured", lambda self: False)
    monkeypatch.setattr(message_service.SalesforceAdapter, "configured", lambda self: False)

    result = message_service.collect_messages()

    # 只有钉钉补示例：OA / Salesforce 在消息中心只显示连接状态，
    # 业务明细在各自的看板里（见 message_service._collect_messages_uncached）
    assert result["using_placeholder"] is True
    assert len(result["items"]) == 1
    assert all(item["live"] is False for item in result["items"])
    assert {item["source"] for item in result["items"]} == {"dingtalk"}


def test_collect_messages_never_raises_when_adapter_explodes(monkeypatch) -> None:
    """某一家抛异常时，其余来源照常返回。"""
    monkeypatch.setattr(message_service.DingTalkAdapter, "configured", lambda self: True)
    monkeypatch.setattr(message_service.OaAdapter, "configured", lambda self: True)
    monkeypatch.setattr(message_service.SalesforceAdapter, "configured", lambda self: True)

    def boom(self, user_id=""):
        raise RuntimeError("外部接口炸了")

    monkeypatch.setattr(message_service.OaAdapter, "fetch", boom)

    def ok(self, user_id=""):
        return [
            MessageItem(
                source="dingtalk",
                external_id="t1",
                title="钉钉真实待办",
                priority=PRIORITY_HIGH,
            )
        ]

    monkeypatch.setattr(message_service.DingTalkAdapter, "fetch", ok)

    result = message_service.collect_messages()

    titles = [item["title"] for item in result["items"]]
    assert "钉钉真实待办" in titles
    oa_state = next(s for s in result["sources"] if s["source"] == "oa")
    assert oa_state["error"] == "外部接口炸了"


def test_items_sorted_by_priority_high_first() -> None:
    items = [
        MessageItem(source="oa", external_id="a", title="低", priority=PRIORITY_LOW),
        MessageItem(source="oa", external_id="b", title="高", priority=PRIORITY_HIGH),
        MessageItem(source="oa", external_id="c", title="中", priority=PRIORITY_MEDIUM),
    ]
    ordered = message_service._sort(items)
    assert [item.title for item in ordered] == ["高", "中", "低"]


def test_dedupe_drops_same_external_id() -> None:
    items = [
        MessageItem(source=SOURCE_OA, external_id="x", title="同一条"),
        MessageItem(source=SOURCE_OA, external_id="x", title="同一条"),
    ]
    assert len(message_service._dedupe(items)) == 1


def test_normalized_exposes_display_fields() -> None:
    item = MessageItem(source="oa", external_id="1", title="  审批单  ", detail="  流程 ", priority="高")
    data = item.normalized()
    assert data["title"] == "审批单"
    assert data["detail"] == "流程"
    assert data["source_label"] == "公司 OA"
    assert data["tone"] == "green"
    assert data["priority"] == PRIORITY_HIGH


@pytest.mark.parametrize("path", ["result", "result.todos"])
def test_oa_rows_path_configuration(path: str, monkeypatch) -> None:
    """OA 列表路径可配置，换路径不需要改代码。"""
    from app.services import integration_config
    from app.services.oa_messages import OaAdapter

    values = {
        "base_url": "https://oa.example.com",
        "auth_mode": "token",
        "token": "t",
        "rows_path": path,
        "field_map": '{"title":"subject","creator":"creator"}',
        "default_title": "OA 待办",
        "timeout_seconds": 10,
        "max_items": 30,
    }
    monkeypatch.setattr(
        integration_config,
        "effective_settings",
        lambda: {"dingtalk": {}, "oa": values, "salesforce": {}},
    )

    adapter = OaAdapter()
    payload = {"result": {"todos": [{"subject": "买试剂", "creator": "张三"}]}}
    items = adapter._map_rows(payload)

    if path == "result":
        assert items == []  # result 是 dict 不是 list，会被跳过
    else:
        assert len(items) == 1
        assert items[0].title == "买试剂"
        assert "张三" in items[0].detail


# ---------- 钉钉 userid 解析（无 CorpId 的替代路径） ----------


def test_resolve_user_id_uses_mobile_lookup(monkeypatch) -> None:
    """用手机号换 userid：不需要 CorpId，走 topapi/v2/user/get。"""
    from app.services.dingtalk_messages import DingTalkAdapter

    adapter = DingTalkAdapter()
    monkeypatch.setattr(adapter, "_post_oapi", lambda path, payload: {
        "errcode": 0,
        "result": {"userid": "zhangsan001"},
    })

    assert adapter.resolve_user_id("13800000000") == "zhangsan001"


def test_resolve_user_id_rejects_empty_mobile() -> None:
    from app.services.dingtalk_messages import DingTalkAdapter

    with pytest.raises(ValueError):
        DingTalkAdapter().resolve_user_id("  ")


def test_resolve_user_id_raises_when_not_found(monkeypatch) -> None:
    from app.services.dingtalk_messages import DingTalkAdapter

    adapter = DingTalkAdapter()
    monkeypatch.setattr(adapter, "_post_oapi", lambda path, payload: {
        "errcode": 0,
        "result": {},
    })

    with pytest.raises(RuntimeError):
        adapter.resolve_user_id("13800000000")


def test_userid_endpoint_returns_ok(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from app.api.messages import router
    from app.services.dingtalk_messages import DingTalkAdapter

    monkeypatch.setattr(
        DingTalkAdapter,
        "resolve_user_id",
        lambda self, mobile, app_key="", app_secret="", corp_id="": "zhangsan001",
    )
    app = FastAPI()
    app.include_router(router, prefix="/api")
    client = TestClient(app)

    resp = client.post("/api/messages/dingtalk/userid", json={"mobile": "13800000000"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["user_id"] == "zhangsan001"


def test_userid_endpoint_reports_failure(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from app.api.messages import router
    from app.services.dingtalk_messages import DingTalkAdapter

    def boom(self, mobile, app_key="", app_secret="", corp_id=""):
        raise RuntimeError("不合法的appKey或appSecret")

    monkeypatch.setattr(DingTalkAdapter, "resolve_user_id", boom)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    client = TestClient(app)

    resp = client.post("/api/messages/dingtalk/userid", json={"mobile": "138"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "appKey" in body["detail"]


# ---------- 保存配置后首页内容消失的回归测试 ----------


def test_partial_config_keeps_other_sources_placeholders(monkeypatch) -> None:
    """只配好钉钉时，OA / Salesforce 的示例数据必须保留。

    之前是"只要有一家配了就全撤示例"，结果保存配置后首页直接空掉，
    看起来就像"配置好的东西又不见了"。
    """
    monkeypatch.setattr(message_service.DingTalkAdapter, "configured", lambda self: True)
    monkeypatch.setattr(message_service.OaAdapter, "configured", lambda self: False)
    monkeypatch.setattr(message_service.SalesforceAdapter, "configured", lambda self: False)

    def dingtalk_ok(self, user_id=""):
        return [MessageItem(source="dingtalk", external_id="t1", title="钉钉真实待办")]

    monkeypatch.setattr(message_service.DingTalkAdapter, "fetch", dingtalk_ok)
    monkeypatch.setattr(message_service.OaAdapter, "fetch", lambda self, user_id="": [])
    monkeypatch.setattr(message_service.SalesforceAdapter, "fetch", lambda self, user_id="": [])

    result = message_service.collect_messages()

    titles = [item["title"] for item in result["items"]]
    assert "钉钉真实待办" in titles
    # OA / Salesforce 不补示例了：明细在各自业务看板，消息中心只显示连接状态
    assert "审批智慧实验室月度报告" not in titles
    assert "跟进重点客户服务反馈" not in titles
    # 钉钉已配置 → 它不补示例；OA / Salesforce 现在也不补示例，
    # 所以整页没有占位数据（明细在各自业务看板里）
    assert result["using_placeholder"] is False
    # 钉钉那条是真实数据，不能被标成示例
    live_map = {item["title"]: item["live"] for item in result["items"]}
    assert live_map["钉钉真实待办"] is True


def test_all_configured_has_no_placeholder(monkeypatch) -> None:
    """三家都配好时不该再返回任何示例数据。"""
    monkeypatch.setattr(message_service.DingTalkAdapter, "configured", lambda self: True)
    monkeypatch.setattr(message_service.OaAdapter, "configured", lambda self: True)
    monkeypatch.setattr(message_service.SalesforceAdapter, "configured", lambda self: True)

    def one(self, user_id=""):
        return [MessageItem(source="dingtalk", external_id="t1", title="真实待办")]

    monkeypatch.setattr(message_service.DingTalkAdapter, "fetch", one)
    monkeypatch.setattr(message_service.OaAdapter, "fetch", lambda self, user_id="": [])
    monkeypatch.setattr(message_service.SalesforceAdapter, "fetch", lambda self, user_id="": [])

    result = message_service.collect_messages()

    assert result["using_placeholder"] is False
    assert all(item["live"] is True for item in result["items"])


def test_configured_user_id_falls_back_when_frontend_passes_empty(monkeypatch) -> None:
    """前端没免登到 userid 时，必须回落到设置页配的「默认 userid」。"""
    captured: dict[str, str] = {}

    monkeypatch.setattr(message_service.DingTalkAdapter, "configured", lambda self: True)
    monkeypatch.setattr(message_service.DingTalkAdapter, "configured_user_id", lambda self: "zhangsan001")

    def capture(self, user_id=""):
        captured["user_id"] = user_id
        return []

    monkeypatch.setattr(message_service.DingTalkAdapter, "fetch", capture)
    monkeypatch.setattr(message_service.OaAdapter, "fetch", lambda self, user_id="": [])
    monkeypatch.setattr(message_service.SalesforceAdapter, "fetch", lambda self, user_id="": [])

    message_service.collect_messages(user_id="")

    assert captured["user_id"] == "zhangsan001"


def test_frontend_user_id_wins_over_config(monkeypatch) -> None:
    """前端免登拿到 userid 时优先用前端的。"""
    captured: dict[str, str] = {}

    monkeypatch.setattr(message_service.DingTalkAdapter, "configured", lambda self: True)
    monkeypatch.setattr(message_service.DingTalkAdapter, "configured_user_id", lambda self: "from_config")

    def capture(self, user_id=""):
        captured["user_id"] = user_id
        return []

    monkeypatch.setattr(message_service.DingTalkAdapter, "fetch", capture)
    monkeypatch.setattr(message_service.OaAdapter, "fetch", lambda self, user_id="": [])
    monkeypatch.setattr(message_service.SalesforceAdapter, "fetch", lambda self, user_id="": [])

    message_service.collect_messages(user_id="from_frontend")

    assert captured["user_id"] == "from_frontend"


def test_dingtalk_status_explains_missing_userid(monkeypatch) -> None:
    """已连接但缺 userid 时，角标要能说出原因。"""
    from app.services.dingtalk_messages import DingTalkAdapter

    adapter = DingTalkAdapter()
    monkeypatch.setattr(adapter, "_config", {"app_key": "k", "app_secret": "s", "user_id": ""})

    status = adapter.status()
    assert status["enabled"] is True
    assert "userid" in status["reason"]


def test_dingtalk_status_explains_not_configured(monkeypatch) -> None:
    from app.services.dingtalk_messages import DingTalkAdapter

    adapter = DingTalkAdapter()
    monkeypatch.setattr(adapter, "_config", {"app_key": "", "app_secret": "", "user_id": ""})

    status = adapter.status()
    assert status["enabled"] is False
    assert "未配置" in status["reason"]


def test_notify_and_ding_report_no_query_api(monkeypatch) -> None:
    """工作通知与 DING 钉钉未开放收件箱查询接口。

    这两类不该再去打接口（会返回 Invalid method），
    而是明确标注"未开放查询接口"，让用户知道不是自己配错了。

    它们已从 _KIND_ORDER 移除（首页不展示这类恒为空的板块），
    所以直接验 handler 逻辑。
    """
    from app.services.dingtalk_messages import DingTalkAdapter

    adapter = DingTalkAdapter()
    monkeypatch.setattr(adapter, "_config", {
        "app_key": "k", "app_secret": "s", "user_id": "u",
    })

    def fail_post(path, payload):
        raise AssertionError(f"不该再调接口：{path}")

    monkeypatch.setattr(adapter, "_post_oapi", fail_post)

    assert adapter._fetch_notify() == []
    assert adapter._fetch_ding() == []
    notes = adapter.status()["kind_notes"]
    assert "未开放" in notes["notify"]
    assert "未开放" in notes["ding"]
    # 恒为空的板块不该出现在首页的类型列表里
    assert adapter.enabled_kinds() == [] or "notify" not in adapter.enabled_kinds()


def test_userid_endpoint_accepts_inline_credentials(monkeypatch) -> None:
    """请求体里带凭据时，优先用请求体的（用户可能还没点保存）。"""
    from fastapi.testclient import TestClient

    from app.api.messages import router
    from app.services.dingtalk_messages import DingTalkAdapter

    seen: dict[str, str] = {}

    def fake_resolve(self, mobile, app_key="", app_secret="", corp_id=""):
        seen.update(mobile=mobile, app_key=app_key, app_secret=app_secret, corp_id=corp_id)
        return "zhangsan001"

    monkeypatch.setattr(DingTalkAdapter, "resolve_user_id", fake_resolve)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    client = TestClient(app)

    resp = client.post("/api/messages/dingtalk/userid", json={
        "mobile": "13800000000",
        "app_key": "dingaadldrx3nvfxjceo",
        "app_secret": "secret-value",
    })

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert seen["app_key"] == "dingaadldrx3nvfxjceo"
    assert seen["app_secret"] == "secret-value"


def test_dingtalk_status_exposes_enabled_kinds(monkeypatch) -> None:
    """首页"Ding DIng"分区要按启用的消息类型分子板块。"""
    from app.services.dingtalk_messages import DingTalkAdapter

    adapter = DingTalkAdapter()
    monkeypatch.setattr(adapter, "_config", {
        "app_key": "k", "app_secret": "s", "user_id": "u",
        "message_kinds": "todo,calendar",
    })

    status = adapter.status()
    assert status["kinds"] == ["todo", "calendar"]


def test_dingtalk_drops_kinds_not_in_display_order(monkeypatch) -> None:
    """配置里残留的失效类型（notify/ding/approval/attendance）要被过滤掉。

    这些类型已从 _KIND_ORDER 移除（首页不展示恒为空或无权限的板块），
    但老的 integrations.json 里可能还留着，不该因此在首页冒出空板块。
    """
    from app.services.dingtalk_messages import DingTalkAdapter

    adapter = DingTalkAdapter()
    monkeypatch.setattr(adapter, "_config", {
        "app_key": "k", "app_secret": "s", "user_id": "u",
        "message_kinds": "todo,notify,ding,calendar,approval,attendance",
    })

    assert adapter.enabled_kinds() == ["todo", "calendar"]


def test_dingtalk_enabled_kinds_defaults_to_three(monkeypatch) -> None:
    """首页只展示真能拉到数据的 3 类：待办任务 / 个人日程 / 部门行程。

    工作通知与 DING 恒为空（钉钉未开放接口），审批/考勤无权限，都不占板块。
    """
    from app.services.dingtalk_messages import DingTalkAdapter

    adapter = DingTalkAdapter()
    monkeypatch.setattr(adapter, "_config", {"app_key": "k", "app_secret": "s", "user_id": "u"})

    assert adapter.enabled_kinds() == ["todo", "calendar", "trip"]


def test_dingtalk_items_carry_kind(monkeypatch) -> None:
    """每条钉钉消息都要带 kind，前端才能归到对应功能板块。"""
    from app.services.dingtalk_messages import DingTalkAdapter

    adapter = DingTalkAdapter()
    monkeypatch.setattr(adapter, "_config", {
        "app_key": "k", "app_secret": "s", "user_id": "u",
        "message_kinds": "todo",
    })
    # 待办走 v1.0：POST todo/users/{unionId}/org/tasks/query，响应顶层平铺
    monkeypatch.setattr(adapter, "resolve_union_id", lambda user_id: "union-1")
    monkeypatch.setattr(adapter, "_post_v1", lambda path, payload: {
        "requestId": "r1",
        "todoCards": [{
            "taskId": "t1", "subject": "整理检测报告", "creatorId": "zhangsan",
            "dueTime": 0, "priority": 1, "pcUrl": "https://example.com/t1",
        }],
    })

    items = adapter.fetch("u")
    assert items
    assert items[0].kind == "todo"
    assert items[0].normalized()["kind"] == "todo"
    assert items[0].title == "整理检测报告"
    assert items[0].url == "https://example.com/t1"


def test_dingtalk_calendar_uses_v1_path(monkeypatch) -> None:
    """日程走 v1.0 且以 unionId 为路径参数，响应是顶层平铺的 events。"""
    from app.services.dingtalk_messages import DingTalkAdapter

    from datetime import datetime, timedelta, timezone

    tomorrow = (datetime.now(timezone.utc).astimezone() + timedelta(days=1)).date()

    adapter = DingTalkAdapter()
    monkeypatch.setattr(adapter, "_config", {
        "app_key": "k", "app_secret": "s", "user_id": "u",
        "message_kinds": "calendar",
    })
    monkeypatch.setattr(adapter, "resolve_union_id", lambda user_id: "union-9")
    seen: dict[str, object] = {}

    def fake_get(path, params):
        seen["path"] = path
        seen["params"] = params
        return {
            "requestId": "r2",
            "events": [{
                "id": "e1",
                "summary": "实验室运营管理部PPT",
                "start": {"dateTime": f"{tomorrow.isoformat()}T15:00:00+08:00"},
                "end": {"dateTime": f"{tomorrow.isoformat()}T16:30:00+08:00"},
                "organizer": {"displayName": "孟鹏杰", "id": "union-9"},
            }],
        }

    monkeypatch.setattr(adapter, "_get_v1", fake_get)

    items = adapter.fetch("u")

    assert seen["path"] == "calendar/users/union-9/calendars/primary/events"
    # 钉钉要求 ISO8601 带冒号时区，Python 的 %z 出的 +0800 会被拒
    assert "+08:00" in str(seen["params"]["timeMin"])
    assert items
    assert items[0].kind == "calendar"
    assert items[0].title == "实验室运营管理部PPT"
    assert items[0].creator == "孟鹏杰"


def test_calendar_start_is_dict_not_stringified(monkeypatch) -> None:
    """日程的 start/end 是 {dateTime,timeZone} 结构，不能被 str() 化。

    之前用 _first() 取 start 会把整个字典变成字符串，
    导致解析日程时抛 AttributeError。
    """
    from app.services.dingtalk_messages import DingTalkAdapter

    # 明天 15:00 —— 动态算，别把日期写死，过一天就挂
    from datetime import datetime, timedelta, timezone

    tomorrow = (datetime.now(timezone.utc).astimezone() + timedelta(days=1)).date()
    start_at = f"{tomorrow.isoformat()}T15:00:00+08:00"
    end_at = f"{tomorrow.isoformat()}T16:30:00+08:00"

    adapter = DingTalkAdapter()
    monkeypatch.setattr(adapter, "_config", {
        "app_key": "k", "app_secret": "s", "user_id": "u",
        "message_kinds": "calendar",
    })
    monkeypatch.setattr(adapter, "resolve_union_id", lambda user_id: "union-9")
    monkeypatch.setattr(adapter, "_get_v1", lambda path, params: {
        "events": [{
            "id": "e1",
            "summary": "部门会议",
            "start": {"dateTime": start_at, "timeZone": "Asia/Shanghai"},
            "end": {"dateTime": end_at, "timeZone": "Asia/Shanghai"},
            "organizer": {"displayName": "孟鹏杰"},
        }],
    })

    items = adapter.fetch("u")

    assert items
    assert items[0].title == "部门会议"
    assert items[0].due == "明天 15:00"
    assert "16:30" in items[0].detail


def test_resolve_union_id_uses_userid_lookup(monkeypatch) -> None:
    """v1.0 的待办/日程要 unionId，靠 topapi/v2/user/get 传 userid 换取。"""
    from app.services.dingtalk_messages import DingTalkAdapter

    adapter = DingTalkAdapter()
    seen: dict[str, object] = {}

    def fake_post(path, payload):
        seen["path"] = path
        seen["payload"] = payload
        return {"errcode": 0, "result": {"unionid": "union-42", "name": "孟鹏杰"}}

    monkeypatch.setattr(adapter, "_post_oapi", fake_post)

    assert adapter.resolve_union_id("u-1") == "union-42"
    assert seen["path"] == "topapi/v2/user/get"
    assert seen["payload"] == {"userid": "u-1"}


def test_resolve_union_id_caches_per_user(monkeypatch) -> None:
    """同一次刷新里只换算一次 unionId。"""
    from app.services.dingtalk_messages import DingTalkAdapter

    adapter = DingTalkAdapter()
    calls: list[str] = []

    def fake_post(path, payload):
        calls.append(payload["userid"])
        return {"errcode": 0, "result": {"unionid": "union-42"}}

    monkeypatch.setattr(adapter, "_post_oapi", fake_post)

    adapter.resolve_union_id("u-1")
    adapter.resolve_union_id("u-1")
    assert calls == ["u-1"]


def test_v1_error_is_unwrapped_with_message() -> None:
    """v1.0 网关用 code + message 报错，要能翻译出来。"""
    from app.services.dingtalk_messages import _unwrap

    with pytest.raises(RuntimeError) as excinfo:
        _unwrap({
            "code": "Forbidden.AccessDenied.AccessTokenPermissionDenied",
            "message": "应用尚未开通所需的权限：[Custom.Todo.Read]",
        }, "todo/users/union-1/org/tasks/query")

    text = str(excinfo.value)
    assert "Custom.Todo.Read" in text
    assert "AccessTokenPermissionDenied" in text


def test_dingtalk_kind_note_records_failure(monkeypatch) -> None:
    """某一类拉取失败时，首页板块要能说明原因。"""
    from app.services.dingtalk_messages import DingTalkAdapter

    adapter = DingTalkAdapter()
    monkeypatch.setattr(adapter, "_config", {
        "app_key": "k", "app_secret": "s", "user_id": "u",
        "message_kinds": "todo",
    })
    monkeypatch.setattr(adapter, "resolve_union_id", lambda user_id: "union-1")

    def boom(path, payload):
        raise RuntimeError("todo/users/union-1/org/tasks/query 返回错误：权限不足")

    monkeypatch.setattr(adapter, "_post_v1", boom)

    items = adapter.fetch("u")

    assert items == []
    note = adapter.status()["kind_notes"]["todo"]
    assert "权限不足" in note


def _today_str(offset_days: int = 0) -> str:
    """今天（可偏移）的日期字符串，给行程测试造"覆盖今天"的假数据。"""
    from datetime import datetime, timedelta

    return (datetime.now().astimezone() + timedelta(days=offset_days)).date().isoformat()


def test_dingtalk_trip_reads_subscribed_calendars(monkeypatch) -> None:
    """部门行程：先列日历，再按 calendarId 逐个取事件。

    实测该账号订阅了「实验室运营管理部-行程日历」，
    全天日程用 start.date（不是 dateTime）。
    """
    from app.services.dingtalk_messages import DingTalkAdapter

    adapter = DingTalkAdapter()
    monkeypatch.setattr(adapter, "_config", {
        "app_key": "k", "app_secret": "s", "user_id": "u",
        "message_kinds": "trip",
    })
    monkeypatch.setattr(adapter, "resolve_union_id", lambda user_id: "union-9")

    calls: list[str] = []

    def fake_get(path, params):
        calls.append(path)
        if path.endswith("/calendars"):
            return {"response": {"calendars": [
                {"id": "primary", "summary": "我的日历", "type": "primary"},
                {"id": "cal-77", "summary": "实验室运营管理部-行程日历", "type": "subscribed"},
            ]}}
        # 只展示"覆盖今天"的行程，所以日期必须围绕今天构造
        return {"events": [{
            "id": "t1",
            "summary": "阿坝州市人民医院-6s实施、运营回访",
            "isAllDay": True,
            # 全天日程按半开区间 [start, end) 判"覆盖今天"，end 是排他的，
            # 所以要跨天（昨天开始、明天结束）才算覆盖今天
            "start": {"date": _today_str(-1)},
            "end": {"date": _today_str(1)},
            "organizer": {"displayName": "邱宇", "self": False},
        }]}

    monkeypatch.setattr(adapter, "_get_v1", fake_get)

    items = adapter.fetch("u")

    assert calls[0] == "calendar/users/union-9/calendars"
    assert calls[1] == "calendar/users/union-9/calendars/cal-77/events"
    # 部门行程只来自 subscribed 日历（cal-77）。
    # 注意 primary 现在**会**被查，但那是 _resolve_trip_creators 反查
    # "行程创建人"用的，不是把 primary 当部门行程来源。
    assert len(items) == 1
    assert items[0].kind == "trip"
    assert items[0].title == "阿坝州市人民医院-6s实施、运营回访"
    assert "实验室运营管理部-行程日历" in items[0].detail
    assert items[0].extra["all_day"] is True
    # 创建人不再直接取 event.organizer：钉钉日程接口不给创建人，
    # 现在由 _resolve_trip_creators 批量扫描同事日历来反查
    # （那个函数有独立测试），所以这里只验证字段结构。
    assert "trip_creator" in items[0].extra
    assert items[0].extra["calendar"] == "实验室运营管理部-行程日历"


def test_dingtalk_trip_notes_when_no_subscribed_calendar(monkeypatch) -> None:
    """没订阅任何部门日历时给说明，而不是静默返回空。"""
    from app.services.dingtalk_messages import DingTalkAdapter

    adapter = DingTalkAdapter()
    monkeypatch.setattr(adapter, "_config", {
        "app_key": "k", "app_secret": "s", "user_id": "u",
        "message_kinds": "trip",
    })
    monkeypatch.setattr(adapter, "resolve_union_id", lambda user_id: "union-9")
    monkeypatch.setattr(adapter, "_get_v1", lambda path, params: {
        "response": {"calendars": [{"id": "primary", "summary": "我的日历", "type": "primary"}]}
    })

    assert adapter.fetch("u") == []
    assert "部门日历" in adapter.status()["kind_notes"]["trip"]


def test_dingtalk_approval_maps_status(monkeypatch) -> None:
    """审批走 topapi/processinstance/list，状态码转中文。

    审批暂未在界面展示，但解析逻辑保留，权限开通后加回 _KIND_ORDER 即可。
    """
    from app.services.dingtalk_messages import DingTalkAdapter

    adapter = DingTalkAdapter()
    monkeypatch.setattr(adapter, "_config", {
        "app_key": "k", "app_secret": "s", "user_id": "u1",
    })
    seen: dict[str, object] = {}

    def fake_post(path, payload):
        seen["path"] = path
        seen["payload"] = payload
        return {"errcode": 0, "result": {"list": [{
            "process_instance_id": "pi-1",
            "title": "采购申请",
            "status": "RUNNING",
            "originator_name": "孟鹏杰",
        }]}}

    monkeypatch.setattr(adapter, "_post_oapi", fake_post)

    # 审批暂不在 _KIND_ORDER 内（界面已移除），直接验解析逻辑
    items = adapter._fetch_approval("u1")

    assert seen["path"] == "topapi/processinstance/list"
    assert seen["payload"]["userId"] == "u1"
    assert len(items) == 1
    assert items[0].kind == "approval"
    assert items[0].title == "采购申请"
    assert "审批中" in items[0].detail


def test_dingtalk_attendance_uses_path_without_topapi(monkeypatch) -> None:
    """考勤路径没有 topapi 前缀（历史遗留），别顺手加上去。"""
    from app.services.dingtalk_messages import DingTalkAdapter

    adapter = DingTalkAdapter()
    monkeypatch.setattr(adapter, "_config", {
        "app_key": "k", "app_secret": "s", "user_id": "u1",
    })
    seen: dict[str, object] = {}

    def fake_post(path, payload):
        seen["path"] = path
        return {"errcode": 0, "result": {"attendance_records": [{
            "id": "a1",
            "work_date": "2026-09-20",
            "check_in_time": "2026-09-20 08:57:00",
            "check_out_time": "2026-09-20 18:20:00",
            "is_late": False,
        }]}}

    monkeypatch.setattr(adapter, "_post_oapi", fake_post)

    items = adapter._fetch_attendance("u1")

    assert seen["path"] == "attendance/list"
    assert len(items) == 1
    assert items[0].kind == "attendance"
    assert "上班 08:57" in items[0].detail
    assert "下班 18:20" in items[0].detail



def test_all_day_event_due_has_no_clock(monkeypatch) -> None:
    """全天日程只给 `date` 字段，硬解析会补个假钟点。

    之前显示成「今天 09:00」，应该显示「今天 全天」。
    """
    from app.services.dingtalk_messages import DingTalkAdapter

    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).astimezone().date()

    adapter = DingTalkAdapter()
    monkeypatch.setattr(adapter, "_config", {
        "app_key": "k", "app_secret": "s", "user_id": "u",
        "message_kinds": "calendar",
    })
    monkeypatch.setattr(adapter, "resolve_union_id", lambda user_id: "union-9")
    monkeypatch.setattr(adapter, "_get_v1", lambda path, params: {
        "events": [{
            "id": "e1",
            "summary": "眉山中6s调研与文化墙沟通",
            "isAllDay": True,
            "start": {"date": today.isoformat()},
            "end": {"date": today.isoformat()},
            "organizer": {"displayName": "邱宇"},
        }]
    })

    items = adapter.fetch("u")

    assert items[0].due == "今天 全天"
