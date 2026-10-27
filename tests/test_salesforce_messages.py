"""Salesforce 适配器测试。

重点覆盖：
- TOTP 生成符合 RFC 6238 官方向量（密钥、算法、步长都不能错）
- 登录走「密码 → TOTP → 会话」三步，任一步失败都要给出能看懂的提示
- 查询结果归一化（Task / Event）
- 未配置时首页不显示脏数据
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest


# ---------- TOTP（RFC 6238） ----------


def test_totp_matches_rfc6238_reference_vectors() -> None:
    """RFC 6238 附录 B 的官方向量（SHA1 / 8 位密钥 12345678901234567890）。

    这是唯一可信的验证方式：算法、步长、截断方式错一个就对不上。
    """
    from app.services.salesforce_messages import totp_code

    # base32("12345678901234567890")
    key = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    vectors = [
        (59, "94287082"),
        (1111111109, "07081804"),
        (1111111111, "14050471"),
        (1234567890, "89005924"),
        (2000000000, "69279037"),
        (20000000000, "65353130"),
    ]
    for timestamp, expected in vectors:
        moment = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        assert totp_code(key, moment, digits=8) == expected, f"t={timestamp}"


def test_totp_produces_six_digits_and_rejects_bad_secret() -> None:
    from app.services.salesforce_messages import totp_code

    code = totp_code("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")
    assert len(code) == 6
    assert code.isdigit()

    with pytest.raises(ValueError):
        totp_code("")
    with pytest.raises(ValueError):
        totp_code("不是 base32 的东西!!")


# ---------- 配置判定 ----------


def _adapter(monkeypatch, **overrides):
    from app.services.salesforce_messages import SalesforceAdapter

    base = {
        "auth_mode": "totp",
        "instance_url": "https://maccuracn.my.sfcrmproducts.cn",
        "api_version": "v62.0",
        "org_id": "00DC8000009rygr",
        "username": "myun@maccura.com",
        "password": "pwd",
        "totp_secret": "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ",
        "access_token": "",
        "client_id": "",
        "client_secret": "",
        "refresh_token": "",
        "token": "",
        "task_soql": "",
        "event_soql": "",
        "timeout_seconds": 20,
        "max_items": 30,
    }
    base.update(overrides)
    adapter = SalesforceAdapter()
    monkeypatch.setattr(adapter, "_settings", base)
    return adapter


def test_configured_requires_all_three_totp_parts(monkeypatch) -> None:
    assert _adapter(monkeypatch).configured() is True
    assert _adapter(monkeypatch, totp_secret="").configured() is False
    assert _adapter(monkeypatch, password="").configured() is False
    assert _adapter(monkeypatch, username="").configured() is False
    assert _adapter(monkeypatch, instance_url="").configured() is False


def test_status_explains_what_is_missing(monkeypatch) -> None:
    """缺凭据时要说清缺什么。

    文案按当前设计：6 位动态码是登录时手输的一次性凭证，**不落盘**，
    所以 totp_secret 常态为空，提示语是"动态口令"而不是"TOTP 密钥"。
    """
    status = _adapter(monkeypatch, totp_secret="").status()
    assert status["enabled"] is False
    assert "动态口令" in str(status["reason"])


def test_oauth_mode_accepts_client_credentials(monkeypatch) -> None:
    adapter = _adapter(
        monkeypatch,
        auth_mode="oauth",
        totp_secret="",
        password="",
        client_id="cid",
        client_secret="csec",
    )
    assert adapter.configured() is True


def test_fetch_returns_empty_when_not_configured(monkeypatch) -> None:
    adapter = _adapter(monkeypatch, totp_secret="")
    assert adapter.fetch() == []


# ---------- 登录流程 ----------


def test_login_flows_through_totp_page(monkeypatch) -> None:
    """密码登录 → 拿到 TOTP 表单 → 回填动态口令 → 拿到会话。"""
    from app.services.salesforce_messages import _TOTP_PATH

    adapter = _adapter(monkeypatch)
    seen: dict[str, object] = {}

    TOTP_URL = "https://x/_ui/identity/verification/method/TotpVerificationUi/e?vcsrf=abc"

    class FakeResponse:
        def __init__(self, text: str, url: str = TOTP_URL) -> None:
            self.text = text
            self.url = url
            self.status_code = 200
            self.history = []

    totp_page = (
        '<form action="/_ui/identity/verification/method/TotpVerificationUi/e">'
        '<input type="hidden" name="_CONFIRMATIONTOKEN" value="TOK">'
        '<input type="hidden" name="vcsrf" value="VCSRF">'
        '<input type="text" name="tc" id="tc">'
        '<input type="submit" name="save" value=" 验证 ">'
        "</form>"
    )

    def fake_post(url, data=None, headers=None, **kwargs):
        seen["url"] = url
        seen["data"] = data
        if "/_ui/identity/verification/method/TotpVerificationUi/e" in str(url):
            return FakeResponse("<html>首页</html>")
        return FakeResponse(totp_page, TOTP_URL)

    monkeypatch.setattr(adapter._client, "post", fake_post)
    monkeypatch.setattr(adapter._client.cookies, "get", lambda key, default=None: "sid-value")

    adapter._login_totp()

    assert adapter._authenticated is True
    # 必须 POST 到带 ?vcsrf=... 的**完整 URL**：
    # 只 POST 裸 action 路径会丢掉路由参数，Salesforce 判「验证尝试已超时」，
    # 即使 6 位码完全正确也永远失败。
    assert "/_ui/identity/verification/method/TotpVerificationUi/e" in seen["url"]
    assert seen["data"]["_CONFIRMATIONTOKEN"] == "TOK"
    assert seen["data"]["vcsrf"] == "VCSRF"
    assert len(seen["data"]["tc"]) == 6
    assert seen["data"]["tc"].isdigit()


def test_login_reports_wrong_totp_code(monkeypatch) -> None:
    """动态口令错了要明确报错，不能静默返回空。"""
    adapter = _adapter(monkeypatch)

    class FakeResponse:
        status_code = 200
        url = "https://x/_ui/identity/verification/method/TotpVerificationUi/e?vcsrf=abc"
        history = []

        def __init__(self, text: str) -> None:
            self.text = text

    def fake_post(url, data=None, headers=None, **kwargs):
        if "/_ui/identity/verification/method/TotpVerificationUi/e" in str(url):
            return FakeResponse("<h2>验证您的身份</h2> 您的动态口令无效或已过期")
        return FakeResponse('<input type="text" name="tc" id="tc">')

    monkeypatch.setattr(adapter._client, "post", fake_post)

    with pytest.raises(RuntimeError, match="动态口令"):
        adapter._login_totp()


def test_login_reports_password_failure(monkeypatch) -> None:
    """密码错误时给出明确提示，不要笼统报错。"""
    adapter = _adapter(monkeypatch)

    class FakeResponse:
        status_code = 200
        url = "https://x/"
        text = "<p>您的用户名和密码不匹配</p>"

    monkeypatch.setattr(adapter._client, "post", lambda *a, **k: FakeResponse())

    with pytest.raises(RuntimeError):
        adapter._login_totp()


# ---------- 归一化 ----------


def test_task_record_is_normalized(monkeypatch) -> None:
    adapter = _adapter(monkeypatch)
    item = adapter._task_item({
        "Id": "00T5g000001Abc",
        "Subject": "回访阿坝州人民医院",
        "ActivityDate": "2026-10-25",
        "Status": "In Progress",
        "Priority": "High",
        "Description": "6S 实施回访",
    })
    assert item.source == "salesforce"
    assert item.title == "回访阿坝州人民医院"
    assert "进行中" in item.detail
    assert "6S 实施回访" in item.detail
    assert item.priority == "高"
    assert item.kind == "task"
    assert item.category == "待办"
    assert "00T5g000001Abc" in item.url


def test_event_record_is_normalized(monkeypatch) -> None:
    adapter = _adapter(monkeypatch)
    item = adapter._event_item({
        "Id": "00U5g000001Xyz",
        "Subject": "实验室质量例会",
        "StartDateTime": "2026-10-22T10:00:00+08:00",
        "EndDateTime": "2026-10-22T11:30:00+08:00",
        "IsAllDayEvent": False,
        "Location": "3 楼会议室",
    })
    assert item.title == "实验室质量例会"
    assert "3 楼会议室" in item.detail
    assert "至 11:30" in item.detail
    assert item.kind == "event"
    assert item.category == "日程"


def test_task_status_falls_back_to_raw_value(monkeypatch) -> None:
    adapter = _adapter(monkeypatch)
    item = adapter._task_item({"Subject": "X", "Status": "Weird Value"})
    assert "Weird Value" in item.detail


# ---------- 实战踩坑固化（2026-10-21 实测） ----------


def test_rest_auth_must_use_bearer_header(monkeypatch) -> None:
    """Salesforce REST 只认 Authorization: Bearer <sid>，光带 sid cookie 会 401。

    实测：cookie 里带 sid → INVALID_SESSION_ID；
    同一 sid 放 Authorization 头 → 正常返回 records。
    """
    adapter = _adapter(monkeypatch, auth_mode="session", access_token="sid-abc")
    seen: dict[str, object] = {}

    def fake_get(url, params=None, headers=None, **kwargs):
        seen["headers"] = headers
        seen["url"] = url
        return type("R", (), {"status_code": 200, "text": "{}", "json": lambda self: {"records": []}})()

    monkeypatch.setattr(adapter._client, "get", fake_get)
    adapter._query("SELECT Id FROM Task")

    assert seen["headers"]["Authorization"] == "Bearer sid-abc"
    assert "/services/data/v62.0/query" in seen["url"]


def test_task_status_open_is_mapped(monkeypatch) -> None:
    """这个 org 的未关闭 Task 的 Status 是 "Open"，不是 "Not Started"。"""
    adapter = _adapter(monkeypatch)
    item = adapter._task_item({"Subject": "X", "Status": "Open"})
    assert "未开始" in item.detail


def test_task_detail_includes_related_asset(monkeypatch) -> None:
    """What 是「仪器型号*序列号*医院」这种资产串，对实验室场景最有信息量。"""
    adapter = _adapter(monkeypatch)
    item = adapter._task_item({
        "Subject": "工程产培标记仪器覆盖率和维修次数失败",
        "Status": "Open",
        "What": {"Name": "100010100001*全自动生化分析仪C 800*14AA9901001**周口东新医院_退机"},
        "Owner": {"Name": "黄冬梅"},
    })
    assert "全自动生化分析仪C 800" in item.detail
    assert "周口东新医院" in item.detail
    assert item.creator == "黄冬梅"
    assert item.extra["what"].startswith("100010100001")


def test_event_soql_uses_datetime_window() -> None:
    """DateTime 字段的过滤值必须带时间，只给日期会 400。"""
    from app.services.salesforce_messages import DEFAULT_EVENT_SOQL, DEFAULT_TASK_SOQL

    assert "{min_datetime}" in DEFAULT_EVENT_SOQL
    assert "{max_datetime}" in DEFAULT_EVENT_SOQL
    # Date 字段用纯日期
    assert "{min_date}" in DEFAULT_TASK_SOQL


def test_format_due_converts_utc_to_local_time() -> None:
    """Salesforce 的 Event 存 UTC（+0000），显示前必须换算成本机时区。

    实测 2026-10-22T09:30:00.000+0000 = 北京 17:30，
    不换算会显示成「明天 09:30」。
    """
    from app.services.message_models import format_due

    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc).astimezone()
    # 造一个"明天"的 UTC 时间戳，期望显示的钟点 = UTC+8
    target_utc = (now + timedelta(days=1)).astimezone(timezone.utc).replace(
        hour=9, minute=30, second=0, microsecond=0
    )
    text = format_due(target_utc.strftime("%Y-%m-%dT%H:%M:%S.000+0000"))
    assert text.startswith("明天 ")
    local_hour = (target_utc + timedelta(hours=8)).strftime("%H:%M")
    assert text.endswith(local_hour) or text.endswith(f"{int(local_hour[:2]) + 1:02d}:{local_hour[3:]}")


# ---------- 登录端点（2026-10-21） ----------


def test_login_endpoint_reports_missing_totp(monkeypatch) -> None:
    """缺 TOTP 密钥时要说清缺什么，而不是笼统失败。"""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import integration_config

    original = integration_config.load_config
    # 造一个「什么都没配」的存储
    monkeypatch.setattr(
        integration_config, "load_config",
        lambda: {**original(), "salesforce": {}},
    )

    with TestClient(app) as client:
        resp = client.post("/api/integrations/salesforce/login", json={"auth_mode": "totp"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert any(k in data["detail"] for k in ("TOTP", "实例地址", "用户名", "密码")), data["detail"]


def test_login_endpoint_reports_wrong_totp(monkeypatch) -> None:
    """动态口令错了要明确说"密钥不对"，用户才知道是密钥问题不是密码问题。"""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services.salesforce_messages import SalesforceAdapter

    def fake_login(self):
        raise RuntimeError("Salesforce 动态口令校验未通过，请检查 TOTP 密钥是否正确")

    monkeypatch.setattr(SalesforceAdapter, "login", fake_login)
    monkeypatch.setattr(
        SalesforceAdapter, "configured", lambda self: True
    )

    with TestClient(app) as client:
        resp = client.post(
            "/api/integrations/salesforce/login",
            json={"auth_mode": "totp", "totp_secret": "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert "动态口令" in data["detail"]


def test_session_cache_avoids_repeated_login(monkeypatch) -> None:
    """进程内会话缓存：命中就不重新走登录流程。

    适配器每次 fetch 都新建，没有缓存的话首页每刷新一次都要登录一遍。
    """
    from app.services import salesforce_messages as sm

    sm._SESSION_CACHE.clear()
    calls: list[int] = []

    class FakeClient:
        def __init__(self) -> None:
            self.cookies = _FakeCookies()

        def post(self, *args, **kwargs):
            calls.append(1)
            raise RuntimeError("不该走到登录")

    class _FakeCookies(dict):
        def set(self, key, value, **kwargs):
            self[key] = value

    adapter = _adapter(monkeypatch, auth_mode="totp")
    monkeypatch.setattr(adapter, "_client", FakeClient())

    sm._SESSION_CACHE[sm._cache_key(adapter._settings)] = ("sid-cached", 9_999_999_999.0)
    adapter._ensure_session()
    assert adapter._session_token() == "sid-cached"
    assert not calls, "命中缓存就不该再发登录请求"
    sm._SESSION_CACHE.clear()


def test_login_endpoint_reuses_stored_secret(monkeypatch) -> None:
    """前端回传的是脱敏后的空串，空值要沿用已保存的密码/密钥。

    否则用户在设置页点「登录」会因为密码被清空而失败。
    """
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import integration_config
    from app.services.salesforce_messages import SalesforceAdapter

    seen: dict[str, object] = {}

    def fake_login(self):
        seen["password"] = self._settings.get("password")
        seen["totp_secret"] = self._settings.get("totp_secret")
        return "sid-new"

    monkeypatch.setattr(SalesforceAdapter, "login", fake_login)

    original = integration_config.load_config
    stored = original()["salesforce"]
    monkeypatch.setattr(
        integration_config, "load_config",
        lambda *a, **k: {
            **original(),
            "salesforce": {**stored, "password": "saved-pwd", "totp_secret": "saved-totp"},
        },
    )
    # 注意 original_save 此时已被 conftest 的守卫包过一层（写到临时副本）
    original_save = integration_config.save_config
    monkeypatch.setattr(
        integration_config,
        "save_config",
        lambda updates, **kwargs: original_save(updates, **kwargs),
    )

    with TestClient(app) as client:
        resp = client.post(
            "/api/integrations/salesforce/login",
            json={"auth_mode": "totp", "password": "", "totp_secret": ""},
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert seen["password"] == "saved-pwd"
    assert seen["totp_secret"] == "saved-totp"


def test_login_endpoint_uses_totp_when_secret_present(monkeypatch) -> None:
    """填了动态口令密钥就必须走完整登录流程，而不是回显旧会话。"""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services.salesforce_messages import SalesforceAdapter

    called: list[str] = []

    def fake_login(self):
        called.append(self._settings.get("auth_mode", ""))
        return "sid-fresh"

    monkeypatch.setattr(SalesforceAdapter, "login", fake_login)

    with TestClient(app) as client:
        resp = client.post(
            "/api/integrations/salesforce/login",
            json={
                "auth_mode": "session",
                "username": "u@example.com",
                "password": "pwd",
                "totp_secret": "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ",
            },
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert called == ["totp"], "有动态口令密钥时必须重新走登录流程"


def test_login_endpoint_reuses_stored_secret(monkeypatch) -> None:
    """前端回传的是脱敏后的空串，空值要沿用已保存的密码/密钥。

    否则用户在设置页点「登录」会因为密码被清空而失败。
    """
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import integration_config
    from app.services.salesforce_messages import SalesforceAdapter

    seen: dict[str, object] = {}

    def fake_login(self):
        seen["password"] = self._settings.get("password")
        seen["totp_secret"] = self._settings.get("totp_secret")
        return "sid-new"

    monkeypatch.setattr(SalesforceAdapter, "login", fake_login)

    original = integration_config.load_config
    stored = original()["salesforce"]
    monkeypatch.setattr(
        integration_config, "load_config",
        lambda *a, **k: {
            **original(),
            "salesforce": {**stored, "password": "saved-pwd", "totp_secret": "saved-totp"},
        },
    )
    # 注意 original_save 此时已被 conftest 的守卫包过一层（写到临时副本）
    original_save = integration_config.save_config
    monkeypatch.setattr(
        integration_config,
        "save_config",
        lambda updates, **kwargs: original_save(updates, **kwargs),
    )

    with TestClient(app) as client:
        resp = client.post(
            "/api/integrations/salesforce/login",
            json={"auth_mode": "totp", "password": "", "totp_secret": ""},
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert seen["password"] == "saved-pwd"
    assert seen["totp_secret"] == "saved-totp"


def test_login_endpoint_uses_totp_when_secret_present(monkeypatch) -> None:
    """填了动态口令密钥就必须走完整登录流程，而不是回显旧会话。"""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services.salesforce_messages import SalesforceAdapter

    called: list[str] = []

    def fake_login(self):
        called.append(self._settings.get("auth_mode", ""))
        return "sid-fresh"

    monkeypatch.setattr(SalesforceAdapter, "login", fake_login)

    with TestClient(app) as client:
        resp = client.post(
            "/api/integrations/salesforce/login",
            json={
                "auth_mode": "session",
                "username": "u@example.com",
                "password": "pwd",
                "totp_secret": "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ",
            },
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert called == ["totp"], "有动态口令密钥时必须重新走登录流程"


# ---------- 浏览器免登跳转（frontdoor） ----------


def test_launch_token_is_single_use() -> None:
    """一次性令牌：消费一次就作废，不能重复用。"""
    from app.services.salesforce_messages import consume_launch_token, issue_launch_token

    token, ttl = issue_launch_token("/007/o")
    assert ttl > 0
    assert consume_launch_token(token) == "/007/o"
    with pytest.raises(ValueError):
        consume_launch_token(token)


def test_launch_token_rejects_unknown_and_expired() -> None:
    """没签发过的令牌、以及过期的令牌，都必须被拒。"""
    import time as _time

    from app.services.salesforce_messages import consume_launch_token, issue_launch_token

    with pytest.raises(ValueError):
        consume_launch_token("never-issued")

    token, _ = issue_launch_token("/00T/e")
    # 手动把它改成已过期
    from app.services import salesforce_messages as sm

    sm._LAUNCH_TOKENS[token] = (_time.monotonic() - 1, "/00T/e")
    with pytest.raises(ValueError):
        consume_launch_token(token)


def test_launch_returl_must_be_relative_and_same_origin() -> None:
    """只放行站内相对路径，挡住开放重定向。"""
    from app.services.salesforce_messages import issue_launch_token

    for bad in (
        "https://evil.example.com/x",
        "//evil.example.com/x",
        "http://evil.example.com",
        "007/o",
        "/../etc/passwd",
        "/a\\b",
    ):
        with pytest.raises(ValueError):
            issue_launch_token(bad)

    token, _ = issue_launch_token("/007/o")
    assert token


def test_launch_redirect_builds_frontdoor_url(monkeypatch) -> None:
    """生成的 URL 必须是 frontdoor + sid + retURL，且 sid 做了 URL 编码。"""
    from app.services.salesforce_messages import SalesforceAdapter

    adapter = SalesforceAdapter()
    adapter._settings = {
        "instance_url": "https://maccuracn.my.sfcrmproducts.cn",
        "auth_mode": "session",
        "access_token": "00DC8000009rygr!AQoAQAbc",
    }
    url = adapter.launch_redirect("/00T/e")
    assert url.startswith("https://maccuracn.my.sfcrmproducts.cn/secur/frontdoor.jsp?")
    assert "sid=00DC8000009rygr%21AQoAQAbc" in url
    assert "retURL=%2F00T%2Fe" in url


def test_launch_redirect_rejects_external_path(monkeypatch) -> None:
    """外部地址不能被拼进 frontdoor URL。"""
    from app.services.salesforce_messages import SalesforceAdapter

    adapter = SalesforceAdapter()
    adapter._settings = {
        "instance_url": "https://maccuracn.my.sfcrmproducts.cn",
        "auth_mode": "session",
        "access_token": "sid",
    }
    with pytest.raises(ValueError):
        adapter.launch_redirect("https://evil.example.com")


def test_launch_endpoints_round_trip(monkeypatch) -> None:
    """POST 拿令牌 -> GET 跳转端点 302 到 frontdoor，Referer 不外泄。"""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import integration_config
    from app.services.salesforce_messages import SalesforceAdapter

    fake_sf = {
        "instance_url": "https://maccuracn.my.sfcrmproducts.cn",
        "auth_mode": "session",
        "access_token": "00DC!sid",
    }
    real_effective = integration_config.effective_settings
    monkeypatch.setattr(
        integration_config, "effective_settings",
        lambda: {**real_effective(), "salesforce": fake_sf},
    )
    monkeypatch.setattr(SalesforceAdapter, "_ensure_session", lambda self: None)

    with TestClient(app) as client:
        issued = client.post("/api/integrations/salesforce/launch", json={"path": "/007/o"})
        assert issued.status_code == 200
        body = issued.json()
        assert body["ok"] is True
        token = body["token"]

        resp = client.get(
            "/api/integrations/salesforce/launch",
            params={"t": token},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["location"].startswith(
            "https://maccuracn.my.sfcrmproducts.cn/secur/frontdoor.jsp?"
        )
        assert resp.headers["referrer-policy"] == "no-referrer"

        # 同一个令牌再消费必须失败（现在返回可读的 HTML 错误页）
        again = client.get(
            "/api/integrations/salesforce/launch",
            params={"t": token},
            follow_redirects=False,
        )
        assert again.status_code == 502
        assert "跳转令牌已失效" in again.text


def test_launch_endpoint_rejects_external_path() -> None:
    """申请令牌时就把外部路径挡掉。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        resp = client.post(
            "/api/integrations/salesforce/launch",
            json={"path": "https://evil.example.com"},
        )
    assert resp.status_code == 400


def test_launch_by_path_redirects_without_token(monkeypatch) -> None:
    """前端点按钮走的是「一次同步导航」：GET ?path=... 直接 302，不经过令牌。"""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import integration_config
    from app.services.salesforce_messages import SalesforceAdapter

    fake_sf = {
        "instance_url": "https://maccuracn.my.sfcrmproducts.cn",
        "auth_mode": "session",
        "access_token": "00DC!sid",
    }
    real_effective = integration_config.effective_settings
    monkeypatch.setattr(
        integration_config, "effective_settings",
        lambda: {**real_effective(), "salesforce": fake_sf},
    )
    monkeypatch.setattr(SalesforceAdapter, "_ensure_session", lambda self: None)

    with TestClient(app) as client:
        resp = client.get(
            "/api/integrations/salesforce/launch",
            params={"path": "/lightning/o/Task/list"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        location = resp.headers["location"]
        assert location.startswith("https://maccuracn.my.sfcrmproducts.cn/secur/frontdoor.jsp?")
        assert "retURL=%2Flightning%2Fo%2FTask%2Flist" in location
        assert resp.headers["referrer-policy"] == "no-referrer"


def test_launch_by_path_rejects_external_path() -> None:
    """外部地址不能被拼进 frontdoor URL。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        resp = client.get(
            "/api/integrations/salesforce/launch",
            params={"path": "https://evil.example.com"},
            follow_redirects=False,
        )
    assert resp.status_code == 502
    assert "evil.example.com" not in resp.text


def test_launch_rejects_cross_site_requests() -> None:
    """外部网站发起的跳转要被挡住（Sec-Fetch-Site: cross-site）。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        resp = client.get(
            "/api/integrations/salesforce/launch",
            params={"path": "/lightning/o/Task/list"},
            headers={"Sec-Fetch-Site": "cross-site"},
            follow_redirects=False,
        )
    assert resp.status_code == 502
    assert "来源不被允许" in resp.text


def test_launch_error_returns_readable_html_not_json() -> None:
    """失败时返回可读的 HTML 页面 —— 跳转发生在新标签页，JSON 等于白屏。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        resp = client.get(
            "/api/integrations/salesforce/launch",
            params={"path": "not-a-relative-path"},
            follow_redirects=False,
        )
    assert resp.status_code == 502
    assert "text/html" in resp.headers["content-type"]
    assert "跳转路径不合法" in resp.text
    assert "返回工作平台" in resp.text
