"""钉钉消息适配器。

覆盖五类来源，每类独立开关（见 config.Settings.dingtalk_message_kinds）：

已实测可用的接口（2026-10-20 实地探测确认）：

- todo       待办任务   POST /v1.0/todo/users/{unionId}/org/tasks/query
- calendar   个人日程   GET  /v1.0/calendar/users/{unionId}/calendars/primary/events
- trip       部门行程   GET  /v1.0/calendar/users/{unionId}/calendars/{calendarId}/events
                       （订阅的部门日历，如「实验室运营管理部-行程日历」；
                        只展示**覆盖今天**的行程，见 _trip_range / _event_covers_day）

钉钉**没有开放**收件箱查询接口的两类（官方文档只有发送侧 API）：

- notify     工作通知   topapi/message/corpconversation/asyncsend_v2 等都是"发送"，
                       没有"列出我收到的通知"的接口
- ding       DING 消息  DING 相关 API 只有发送与撤回，没有收件箱列表

这两类在首页照常显示板块，但会明确标注"钉钉未开放查询接口"，
而不是反复抛 Invalid method 让用户以为是自己配错了。

另有两类**只差权限**（钉钉会返回申请链接），按产品决定暂不在界面展示：
approval（topapi/processinstance/list，需 qyapi_aflow）、
attendance（attendance/list，需 qyapi_get_attendance_data）。
解析代码仍在文件里（`_fetch_approval` / `_fetch_attendance`），
权限开通后把它加回 `_KIND_ORDER` 即可恢复，无需重写。

注意 v1.0 网关（api.dingtalk.com/v1.0）与旧版网关（oapi.dingtalk.com）有四处差异：
1. 鉴权头是 `x-acs-dingtalk-access-token`，不是 query 里的 access_token
2. 成功响应是**顶层平铺**的（`todoCards` / `events`），没有 `result` 包裹
3. 错误响应用 `code` + `message`，不是 `errcode` + `errmsg`
4. 时间参数要 ISO8601 带冒号时区（`+08:00`），`+0800` 会被拒

未配置 appKey/appSecret 时不抛错，返回空列表 + 配置状态，由上层决定是否降级。
"""

from __future__ import annotations

import difflib
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from app.core.database import list_event_creators
from app.services import integration_config
from app.services.message_models import (
    PRIORITY_HIGH,
    PRIORITY_MEDIUM,
    SOURCE_DINGTALK,
    MessageItem,
    format_due,
    normalize_priority,
)

logger = logging.getLogger(__name__)

OAPI = "https://oapi.dingtalk.com"
API_V2 = "https://api.dingtalk.com/v1.0"

# 消息类型的固定展示顺序。首页子板块、设置页开关共用这一份。
# 只列"首页真的拿得到数据"的类型：
# - notify/ding 钉钉未开放收件箱查询接口，恒为空板块，按产品决定不展示
#   （它们的说明文案仍在 _KIND_UNSUPPORTED 里，钉钉开放后加回这里即可）
# - approval/attendance 权限未开通，解析代码保留在文件里
_KIND_ORDER = (
    "todo",
    "calendar",
    "trip",
)

# 每次拉取上限，避免首页被单次大结果拖慢
MAX_PER_KIND = 20

# 部门行程的接口拉取上限：要一次把"今天往前 14 天"的行程都捞回来，
# 再在本地筛"覆盖今天"。留足余量，避免 maxResults 截断（见 _trip_range）。
MAX_TRIP_FETCH = 50

# 部门行程往回看的天数。钉钉的日程接口按**开始时间**过滤，
# 跨天行程如果前天开始、今天还没结束，直接查"今天"会一条都取不到，
# 所以必须放宽窗口再本地筛（见 _trip_range）。
_TRIP_LOOKBACK_DAYS = 14

# 复姓名单：这些姓氏有两个字，取姓氏时不能只取第一个字
_COMPOUND_SURNAMES = frozenset(
    {
        "欧阳", "司马", "上官", "诸葛", "夏侯", "东方", "皇甫", "尉迟", "公孙",
        "慕容", "司徒", "司空", "宇文", "长孙", "令狐", "端木", "鲜于", "闾丘",
        "轩辕", "第五", "万俟", "南宫", "呼延", "澹台", "完颜", "赫连",
    }
)

# 消息类型的中文名，用于错误提示
_KIND_LABELS = {
    "todo": "待办任务",
    "notify": "工作通知",
    "ding": "DING 消息",
    "calendar": "个人日程",
    "trip": "部门行程",
    "approval": "审批",
    "attendance": "考勤",
}

# 钉钉未开放收件箱查询接口的类型。照常占一个板块，但给出明确说明，
# 免得用户看到一堆 Invalid method 以为是自己配错了。
_KIND_UNSUPPORTED = {
    "notify": "钉钉未开放工作通知的收件箱查询接口（官方只有发送接口），该板块暂无数据。",
    "ding": "钉钉未开放 DING 消息的收件箱查询接口（官方只有发送与撤回），该板块暂无数据。",
}

# 提醒日程往后看一个月：只显示"今天"的话，一周内要开的会看不到；
# 一个月是"近期日程"的合理视野，再远就不叫近期了。
_CALENDAR_WINDOW_DAYS = 30


# 头像缓存：openId → 头像 URL。钉钉取头像要 Contact.User.Read 权限，
# 未开通时只探测一次就锁死，避免每轮刷新都对每个人打一次 403。
_AVATAR_CACHE: dict[str, str] = {}
_AVATAR_PERMISSION_DENIED = False
_AVATAR_PERMISSION_NOTE = (
    "头像需要钉钉「通讯录个人信息读权限（Contact.User.Read）」，"
    "当前未开通，先用姓名首字代替。"
)

# 通讯录反查姓名缓存：unionId → 姓名。与头像共用同一个权限探测结果。
_CONTACT_NAME_CACHE: dict[str, str] = {}

# 本平台绑定的人（默认 userid）的姓名，行程创建人回落时用
_SELF_NAME = ""

# 行程创建人识别缓存：事件 id → {"name": 姓名, "source": 来源, "at": 时间戳}
#   source="map"   设置页对照表指定的，长期缓存
#   source="copy"  找到副本判定出来的，长期缓存
_TRIP_CREATOR_CACHE: dict[str, dict[str, Any]] = {}

# 候选人员名单（unionId → 姓名），用来在各人的主日历里找行程副本
_TRIP_CREATOR_PEOPLE: dict[str, str] = {}
_TRIP_CREATOR_PEOPLE_AT = 0.0
_TRIP_CREATOR_PEOPLE_TTL = 3600

# 部门行程的组织者（unionId）。这些人的日历里出现的人就是部门同事，
# 用来把"找创建人"的候选名单从 14 人扩到 92 人（实测值）。
_TRIP_ORGANIZER_IDS = ("o0HGMiPHy4tdKaPMqgMqrwwiEiE",)

# 副本标题相似度阈值。太低会把"绵阳人民医院6s"错配成"德阳人民医院回访复盘会"，
# 实测 0.7 是安全线（真匹配 0.71~1.00，错配 0.50~0.76）。
_TRIP_CREATOR_MIN_SCORE = 0.7


class DingTalkAdapter:
    def __init__(self, overrides: dict[str, str] | None = None) -> None:
        # 读集成配置文件优先于 .env，设置页保存后立即生效，无需重启
        self._config = integration_config.effective_settings()["dingtalk"]
        # 临时覆盖：用户在设置页刚填了凭据但还没保存时，
        # 也能立刻用它做连通性检查，不必先保存再试。
        for key, value in (overrides or {}).items():
            if value:
                self._config[key] = value
        self._token: str = ""
        self._token_expires_at: float = 0.0
        # 拉取过程中的错误，首页角标用来显示"为什么没数据"
        self._errors: list[str] = []
        # 每个类型的补充说明（未开放查询接口、缺权限等），首页板块逐条展示
        self._kind_notes: dict[str, str] = {}
        # userid -> unionId 缓存。v1.0 的待办与日程都要 unionId，
        # 一次首页刷新里换算一次就够，不重复打接口。
        self._union_id: str = ""
        self._union_id_source: str = ""

    # ---------- 凭据 ----------

    def configured(self) -> bool:
        return bool(self._config["app_key"] and self._config["app_secret"])

    def configured_user_id(self) -> str:
        """设置页里配的「默认 userid」。

        前端只有在钉钉容器内才能免登换到 userid，其他场景传空串过来，
        必须回落到这里配的值，否则设置页填了也白填。
        """
        return str(self._config.get("user_id") or "").strip()

    def _access_token(self) -> str:
        """带缓存地取 access_token，避免每次刷新都打接口。

        新版凭据（Client ID/Secret）有两种取法：
        - 有 CorpId：POST /v1.0/oauth2/{corpId}/token（官方推荐）
        - 无 CorpId：GET  /gettoken?appkey=&appsecret=（旧版，不需要 CorpId）
        没有开发者权限时开发者后台会打码 CorpId，旧版这条路能先跑起来。
        """
        import time

        if self._token and time.time() < self._token_expires_at:
            return self._token

        corp_id = (self._config.get("corp_id") or "").strip()
        if corp_id:
            try:
                self._token = self._token_v1(corp_id)
            except Exception:
                # 新版失败时回落到旧版，别因为 CorpId 不对就整个不可用
                self._token = self._token_legacy()
        else:
            self._token = self._token_legacy()

        self._token_expires_at = time.time() + 6600  # 官方给 7200s，保守留 10 分钟余量
        return self._token

    def _token_v1(self, corp_id: str) -> str:
        """新版接口：POST /v1.0/oauth2/{corpId}/token"""
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                f"{API_V2}/oauth2/{corp_id}/token",
                json={
                    "client_id": self._config["app_key"],
                    "client_secret": self._config["app_secret"],
                    "grant_type": "client_credentials",
                },
            )
        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise RuntimeError(f"新版取 token 失败：{data.get('message') or data.get('code') or resp.status_code}")
        return token

    def _token_legacy(self) -> str:
        """旧版接口：GET /gettoken?appkey=&appsecret=，不需要 CorpId"""
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                f"{OAPI}/gettoken",
                params={
                    "appkey": self._config["app_key"],
                    "appsecret": self._config["app_secret"],
                },
            ).json()
        if resp.get("errcode") != 0:
            raise RuntimeError(f"钉钉取 token 失败：{resp.get('errmsg')}")
        return resp["access_token"]

    def _post_oapi(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """旧版网关 POST（access_token 走 query）。"""
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                f"{OAPI}/{path}",
                params={"access_token": self._access_token()},
                json=payload,
            )
        return _unwrap(resp.json(), path)

    def _get_v1(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """v1.0 网关 GET。响应是顶层平铺的，取值别找 result。"""
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                f"{API_V2}/{path}",
                headers={"x-acs-dingtalk-access-token": self._access_token()},
                params=params,
            )
        return _unwrap(resp.json(), path)

    def _post_v1(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """v1.0 网关 POST。响应同样是顶层平铺的。"""
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                f"{API_V2}/{path}",
                headers={"x-acs-dingtalk-access-token": self._access_token()},
                json=payload,
            )
        return _unwrap(resp.json(), path)

    # ---------- 拉取 ----------

    def status(self) -> dict[str, Any]:
        """给首页来源角标用：说明这一家当前处于什么状态。"""
        enabled = self.configured()
        reason = ""
        if not enabled:
            reason = "未配置 Client ID / Client Secret"
        elif self._errors:
            reason = "；".join(self._errors[:2])
        elif not self.configured_user_id():
            reason = "已连接，但缺 userid，待办/日程按人查询需要填 userid"
        return {
            "source": SOURCE_DINGTALK,
            "enabled": enabled,
            "reason": reason,
            # 首页"Ding DIng"分区按这四类分子板块
            "kinds": self.enabled_kinds(),
            # 逐类型说明：哪类没数据、为什么（未开放接口 / 缺权限 / 调用失败）
            "kind_notes": dict(self._kind_notes),
        }

    def resolve_user_id(
        self,
        mobile: str,
        app_key: str = "",
        app_secret: str = "",
        corp_id: str = "",
    ) -> str:
        """用手机号换 userid。

        没有开发者权限就没有 CorpId → 前端免登那条路走不通，
        但"按手机号查成员"只需要 access_token（旧版通道也能拿到），
        因此只填手机号也能把待办/日程拉起来。

        接口是 `topapi/v2/user/getbymobile`（body 传 mobile）。
        注意别用 `topapi/v2/user/get`——那是按 userid 反查用户详情，
        传 mobile 过去会直接报 "Missing required arguments:userid"。

        app_key / app_secret / corp_id 传了就临时覆盖已保存的配置，
        用户在设置页刚填完还没保存时也能直接查。
        """
        mobile = (mobile or "").strip()
        if not mobile:
            raise ValueError("请提供手机号")

        overrides = {
            "app_key": (app_key or "").strip(),
            "app_secret": (app_secret or "").strip(),
            "corp_id": (corp_id or "").strip(),
        }
        adapter = self
        if any(overrides.values()):
            adapter = DingTalkAdapter(overrides=overrides)

        resp = adapter._post_oapi(
            "topapi/v2/user/getbymobile",
            {"mobile": mobile, "support_exclusive_account_search": False},
        )
        result = resp.get("result") or {}
        user_id = str(result.get("userid") or result.get("user_id") or "").strip()
        if not user_id:
            raise RuntimeError(f"未查到该手机号对应的钉钉 userid：{resp.get('errmsg') or '请确认手机号已录入组织通讯录'}")
        return user_id

    def resolve_union_id(self, user_id: str) -> str:
        """userid → unionId。

        v1.0 的待办与日程都以 unionId 为路径参数，userid 换不来，
        得再查一次用户详情。查询用户详情接口是 `topapi/v2/user/get`，
        这里传 userid（不是 mobile）—— 传 mobile 会报缺少 userid 参数。
        """
        user_id = (user_id or "").strip()
        if not user_id:
            raise ValueError("缺少 userid，无法换取 unionId")
        if self._union_id and self._union_id_source == user_id:
            return self._union_id

        resp = self._post_oapi("topapi/v2/user/get", {"userid": user_id})
        result = resp.get("result") or {}
        union_id = str(result.get("unionid") or result.get("unionId") or "").strip()
        if not union_id:
            raise RuntimeError(
                f"未能从 userid 换取 unionId：{resp.get('errmsg') or '请确认该 userid 在通讯录中有效'}"
            )
        self._union_id = union_id
        self._union_id_source = user_id
        return union_id

    def enabled_kinds(self) -> list[str]:
        """当前启用的消息类型（顺序与设置页一致）。

        首页的"Ding DIng"分区要按这些分子板块，
        拿的是同一份配置，避免前后端各写一套开关。
        """
        raw = str(self._config.get("message_kinds") or "todo,calendar,trip")
        kinds = [k.strip() for k in raw.split(",") if k.strip()]
        return [k for k in _KIND_ORDER if k in kinds]

    def fetch(self, user_id: str) -> list[MessageItem]:
        """按开关拉取各类型消息，单类失败不影响其他类型。"""
        if not self.configured() or not user_id:
            return []
        kinds = set(self.enabled_kinds())
        items: list[MessageItem] = []
        # 待办、日程、部门行程都要 unionId，先换算一次复用
        union_id = ""
        if kinds & {"todo", "calendar", "trip"}:
            try:
                union_id = self.resolve_union_id(user_id)
            except Exception as exc:
                for kind in sorted(kinds & {"todo", "calendar", "trip"}):
                    self._kind_notes[kind] = f"身份换算失败：{exc}"
                    self._errors.append(f"{_KIND_LABELS.get(kind, kind)}：{exc}")

        for kind, handler in (
            ("todo", lambda: self._fetch_todo(union_id)),
            ("calendar", lambda: self._fetch_calendar(union_id)),
            ("trip", lambda: self._fetch_trip(union_id)),
            # 以下四类解析代码都保留在文件里，首页暂不展示。
            # notify/ding：钉钉未开放收件箱查询接口，恒为空板块。
            # approval/attendance：权限未开通（需 qyapi_aflow / qyapi_get_attendance_data）。
            # 哪天可用了，把 key 加回 _KIND_ORDER 与下面这行即可恢复，无需重写。
            ("notify", lambda: self._fetch_notify()),
            ("ding", lambda: self._fetch_ding()),
            ("approval", lambda: self._fetch_approval(user_id)),
            ("attendance", lambda: self._fetch_attendance(user_id)),
        ):
            if kind not in kinds:
                continue
            try:
                items.extend(handler())
            except Exception as exc:  # 单类失败降级，不让整页挂掉
                logger.warning("钉钉 %s 拉取失败：%s", kind, exc)
                # 记下来，首页角标上要能看到"配好了但没拉到"的真实原因，
                # 否则用户只会以为配置没保存成功。
                self._kind_notes[kind] = str(exc)
                self._errors.append(f"{_KIND_LABELS.get(kind, kind)}：{exc}")
        return items

    def _fetch_todo(self, union_id: str) -> list[MessageItem]:
        """待办任务：POST /v1.0/todo/users/{unionId}/org/tasks/query

        响应顶层平铺：`{"requestId": "...", "todoCards": [...], "totalCount": 0}`。
        """
        if not union_id:
            return []
        resp = self._post_v1(
            f"todo/users/{union_id}/org/tasks/query",
            {"isDone": False, "maxResults": MAX_PER_KIND},
        )
        cards = resp.get("todoCards") or []
        items = []
        for card in cards[:MAX_PER_KIND]:
            title = _first(card, "subject", "title") or "钉钉待办"
            detail_url = card.get("detailUrl") or {}
            url = (
                card.get("pcUrl")
                or card.get("appUrl")
                or (detail_url.get("pcUrl") if isinstance(detail_url, dict) else "")
                or ""
            )
            items.append(
                MessageItem(
                    source=SOURCE_DINGTALK,
                    external_id=str(card.get("taskId") or ""),
                    title=title,
                    detail=f"钉钉待办 · 创建人 {_first(card, 'creatorId', 'creator') or '—'}",
                    due=format_due(card.get("dueTime") or card.get("startTime")),
                    priority=normalize_priority(card.get("priority")),
                    url=url,
                    category="待办",
                    kind="todo",
                    creator=_first(card, "creatorId", "creator") or "",
                    extra={"kind": "todo", "todoType": card.get("todoType") or ""},
                )
            )
        return items

    def _fetch_notify(self) -> list[MessageItem]:
        """工作通知：钉钉未开放收件箱查询接口，这里给不出数据。

        官方开放的是发送侧接口（topapi/message/corpconversation/asyncsend_v2
        发通知、getsendprogress 查发送进度、getsendresult 查发送结果），
        没有"列出我收到的通知"。所以这里不调接口，直接标注原因。
        """
        self._kind_notes["notify"] = _KIND_UNSUPPORTED["notify"]
        return []

    def _fetch_ding(self) -> list[MessageItem]:
        """DING 消息：钉钉只开放发送与撤回，没有收件箱查询接口。"""
        self._kind_notes["ding"] = _KIND_UNSUPPORTED["ding"]
        return []

    def _fetch_trip(self, union_id: str) -> list[MessageItem]:
        """部门行程：**只展示覆盖今天的行程**（订阅的部门日历）。

        先 GET /v1.0/calendar/users/{unionId}/calendars 拿到所有日历，
        再对每个 type=subscribed 的订阅日历取 events。
        实测该账号订阅了「实验室运营管理部-行程日历」，里面是各医院的
        6S 实施与运营回访行程——比个人日程更贴近日常工作。

        这里只留"覆盖今天"的：包括今天开始的，也包括前几天开始、
        今天仍未结束的跨天行程（钉钉月视图也是这么画的，每天都画一遍）。
        """
        if not union_id:
            return []
        cals_resp = self._get_v1(f"calendar/users/{union_id}/calendars", {"maxResults": 20})
        calendars = (cals_resp.get("response") or {}).get("calendars") or []
        subscribed = [c for c in calendars if c.get("type") == "subscribed" and c.get("id")]
        if not subscribed:
            self._kind_notes["trip"] = "该账号没有订阅任何部门日历，暂无部门行程。"
            return []

        time_min, time_max = _trip_range()
        today = datetime.now(timezone.utc).astimezone().replace(hour=0, minute=0, second=0, microsecond=0)

        # 先收集"覆盖今天"的行程，再一次性批量识别创建人（避免每条都重扫同事日历）
        today_events: list[dict[str, Any]] = []
        cal_names: dict[str, str] = {}
        for cal in subscribed:
            cal_id = str(cal["id"])
            cal_name = str(cal.get("summary") or "部门日历")
            try:
                resp = self._get_v1(
                    f"calendar/users/{union_id}/calendars/{cal_id}/events",
                    {
                        "maxResults": MAX_TRIP_FETCH,
                        "timeMin": time_min,
                        "timeMax": time_max,
                        "sortOrder": "startTime",
                    },
                )
            except Exception as exc:
                # 一个日历拉不到不影响其他日历
                logger.warning("部门日历 %s 拉取失败：%s", cal_name, exc)
                continue
            for event in resp.get("events") or []:
                if not _event_covers_day(event, today):
                    continue
                today_events.append(event)
                cal_names[str(event.get("id") or "")] = cal_name

        # 创建人：钉钉接口不给，靠"谁的主日历里有副本"反查（见 _resolve_trip_creators）
        creators = self._resolve_trip_creators(today_events)

        picked: list[tuple[datetime, MessageItem]] = []
        for event in today_events:
            eid = str(event.get("id") or "")
            creator_name, creator_source = creators.get(eid) or self._trip_creator(event)
            item = _calendar_item(
                event,
                kind="trip",
                category="部门行程",
                default_title="部门行程",
                creator=creator_name,
                show_creator=False,  # 创建人由前端用头像单独展示
            )
            if item is None:
                continue
            cal_name = cal_names.get(eid, "部门日历")
            # 日期标签以"今天"为基准：跨天行程若沿用开始日，
            # format_due 会显示成"昨天 全天"，在"今天的行程"里会误导。
            item.due = _trip_due_label(event, today)
            item.detail = f"{cal_name} · {item.detail}" if item.detail else cal_name
            item.extra["calendar"] = cal_name
            item.extra["trip_creator"] = creator_name
            item.extra["trip_creator_source"] = creator_source
            picked.append((_event_start_dt(event) or today, item))
        # 钉钉的 sortOrder 不保证严格有序，这里按开始时间自己排一遍
        picked.sort(key=lambda pair: pair[0])
        return [item for _, item in picked][:MAX_PER_KIND]

    def _resolve_user_name(self, userid: str, union_id: str = "") -> str:
        """按 userid / unionId 反查姓名（能查到就用，查不到就空）。"""
        userid = (userid or "").strip()
        if userid:
            try:
                resp = self._post_oapi("topapi/v2/user/get", {"userid": userid})
                result = resp.get("result") or {}
                name = str(result.get("name") or "").strip()
                if name:
                    return name
            except Exception:  # noqa: BLE001
                pass
        union_id = (union_id or "").strip()
        if union_id:
            name = self._contact_user_name(union_id)
            if name:
                return name
        return ""

    def _contact_user_name(self, union_id: str) -> str:
        """按 unionId 查姓名（需 Contact.User.Read 权限，未开通时返回空）。"""
        global _AVATAR_PERMISSION_DENIED

        if not union_id:
            return ""
        if union_id in _CONTACT_NAME_CACHE:
            return _CONTACT_NAME_CACHE[union_id]
        if _AVATAR_PERMISSION_DENIED:
            return ""
        try:
            with httpx.Client(timeout=8) as client:
                resp = client.get(
                    f"{API_V2}/contact/users/{union_id}",
                    headers={"x-acs-dingtalk-access-token": self._access_token()},
                )
        except httpx.HTTPError:
            return ""
        if resp.status_code == 403:
            _AVATAR_PERMISSION_DENIED = True
            return ""
        if resp.status_code >= 400:
            return ""
        data = resp.json() if resp.text else {}
        name = str(data.get("name") or data.get("displayName") or "").strip()
        if name:
            _CONTACT_NAME_CACHE[union_id] = name
        return name

    def _trip_creator_map(self) -> dict[str, str]:
        """设置页里配的「行程标题=创建人」对照表。"""
        raw = str(self._config.get("trip_creator_map") or "")
        table: dict[str, str] = {}
        for line in raw.splitlines():
            line = line.strip()
            # 跳过空行和 # 注释行
            if not line or line.startswith("#") or "=" not in line:
                continue
            title, _, name = line.partition("=")
            title = title.strip()
            name = name.strip()
            if title and name:
                table[title] = name
        return table

    def _self_display_name(self) -> str:
        """当前平台绑定的人（本平台默认 userid 对应的人）的姓名。"""
        global _SELF_NAME

        if _SELF_NAME:
            return _SELF_NAME
        user_id = self.configured_user_id()
        if not user_id:
            return ""
        try:
            resp = self._post_oapi("topapi/v2/user/get", {"userid": user_id})
            result = resp.get("result") or {}
            name = str(result.get("name") or "").strip()
        except Exception:  # noqa: BLE001
            name = ""
        if name:
            _SELF_NAME = name
        return name

    def _trip_creator_candidates(self) -> dict[str, str]:
        """候选人员（unionId → 姓名）。

        用于在"谁的主日历里存着这条行程的副本"里找创建人。
        名单有两个来源：① 当前用户自己主日历里的组织者与参与者；
        ② 全部行程的组织者（邱宇）日历里的组织者与参与者——他日历里出现的
        人基本就是实验室运营管理部同事（实测能挖到 92 人、91 个日历可读）。
        只用 ① 名单只有 14 人，会漏掉真正的创建人。
        """
        global _TRIP_CREATOR_PEOPLE, _TRIP_CREATOR_PEOPLE_AT

        now_ts = time.monotonic()
        if _TRIP_CREATOR_PEOPLE and (now_ts - _TRIP_CREATOR_PEOPLE_AT) < _TRIP_CREATOR_PEOPLE_TTL:
            return _TRIP_CREATOR_PEOPLE
        people: dict[str, str] = {}
        try:
            union_id = self.resolve_union_id(self.configured_user_id())
            today = datetime.now(timezone.utc).astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
            window = {
                "maxResults": 100,
                "timeMin": (today - timedelta(days=120)).isoformat(),
                "timeMax": (today + timedelta(days=60)).isoformat(),
                "sortOrder": "startTime",
            }
            resp = self._get_v1(f"calendar/users/{union_id}/calendars/primary/events", window)
            for event in resp.get("events") or []:
                people.update(_people_of_event(event))
            # 行程的组织者（邱宇）日历里出现的人，基本就是实验室运营管理部同事。
            # 只用自己日历里的人只有 14 个，会漏掉真正的创建人（实测扩到 92 人）。
            for organizer_id in _TRIP_ORGANIZER_IDS:
                if not organizer_id:
                    continue
                try:
                    resp = self._get_v1(
                        f"calendar/users/{organizer_id}/calendars/primary/events", window
                    )
                except Exception:  # noqa: BLE001
                    continue
                for event in resp.get("events") or []:
                    people.update(_people_of_event(event))
        except Exception as exc:  # noqa: BLE001
            logger.warning("行程创建人候选名单获取失败：%s", exc)
            return _TRIP_CREATOR_PEOPLE
        if people:
            _TRIP_CREATOR_PEOPLE = people
            _TRIP_CREATOR_PEOPLE_AT = now_ts
        return _TRIP_CREATOR_PEOPLE

    def _trip_creator(self, event: dict[str, Any]) -> tuple[str, str]:
        """这条行程的创建人，返回 (姓名, 来源说明)。只看缓存。

        真正的识别在 _resolve_trip_creators（批量做，一次扫描所有同事的日历）。
        """
        event_id = str(event.get("id") or "")
        cached = _TRIP_CREATOR_CACHE.get(event_id)
        if not cached:
            return ("", "")
        return (cached["name"], cached["source"])

    def _resolve_trip_creators(self, events: list[dict[str, Any]]) -> dict[str, tuple[str, str]]:
        """批量识别一批行程的创建人。

        钉钉开放接口**不返回**每条日程的创建人：日程详情只有 organizer 和
        createTime，"动态"里那条"XX 新建了日程"没有任何接口能取（activities /
        operationLogs 等全部 404，订阅日历的参与者接口直接 PERMISSION_DENIED）。

        但有一个可靠规律：**谁的主日历里存着这条行程的副本，谁就是创建人**。
        实测：
          「省一中6S实施前调研」「西安中医院6s实施」→ 副本在杨娜的主日历
          「绵阳市人民医院回访」→ 副本在孟鹏杰的主日历（钉钉"动态"里显示的
            新建人正是孟鹏杰，createTime 10月12日 10:24 也完全吻合）

        找不到副本时回落为"本平台绑定的人"（部门行程多数由运营专员本人录入），
        来源标记 self，6 小时后再试一次。

        一次扫描覆盖所有待识别的事件：每个候选人只读一次日历，
        避免每条行程都重扫一遍（实测逐条扫要 11 秒，批量只 6 秒）。
        """
        global _TRIP_CREATOR_CACHE

        pending = [
            e for e in events
            if str(e.get("id") or "") and not self._trip_creator(e)[0]
        ]
        if not pending:
            return {}

        # ① 本地已记录的事件创建人（Stream 模式自动采集，精确度最高）
        local = list_event_creators([str(e.get("id")) for e in pending])
        if local:
            for event in pending:
                eid = str(event.get("id"))
                row = local.get(eid)
                if not row:
                    continue
                name = str(row.get("operator_name") or "").strip()
                if not name:
                    name = self._resolve_user_name(
                        str(row.get("operator_userid") or ""),
                        str(row.get("operator_union_id") or ""),
                    )
                if name:
                    resolved[eid] = (name, "stream")
                    _TRIP_CREATOR_CACHE[eid] = {"name": name, "source": "stream", "at": time.monotonic()}
            pending = [e for e in pending if str(e.get("id")) not in resolved]
            if not pending:
                return resolved

        # ② 设置页的「行程标题=创建人」对照表
        table = self._trip_creator_map()
        resolved: dict[str, tuple[str, str]] = {}
        still_pending: list[dict[str, Any]] = []
        for event in pending:
            eid = str(event.get("id"))
            title = str(event.get("summary") or "")
            name = table.get(title)
            if name:
                resolved[eid] = (name, "map")
                _TRIP_CREATOR_CACHE[eid] = {"name": name, "source": "map", "at": time.monotonic()}
            else:
                still_pending.append(event)
        if not still_pending:
            return resolved
        pending = still_pending

        # 一次扫描：把所有候选人在同一条时间窗内的日程捞出来
        days = [d for d in (_event_day(e) for e in pending) if d]
        people = self._trip_creator_candidates()
        copies: list[tuple[str, str, date]] = []   # (主人, 标题, 日期)
        if days and people:
            # 注意：必须是带时区的完整 ISO8601，钉钉不认纯日期（"2026-10-14"），
            # 会报 InvalidParameter.ParsedISO8601TimestampError —— 踩过一次，
            # 异常被吞掉后所有副本都读不到，自动判定等于没生效。
            window_start = _day_start_iso(min(days) - timedelta(days=3))
            window_end = _day_start_iso(max(days) + timedelta(days=4))
            def fetch_one(union_id: str) -> list[dict[str, Any]]:
                try:
                    resp = self._get_v1(
                        f"calendar/users/{union_id}/calendars/primary/events",
                        {
                            "maxResults": 200,
                            "timeMin": window_start,
                            "timeMax": window_end,
                            "sortOrder": "startTime",
                        },
                    )
                    return resp.get("events") or []
                except Exception:  # noqa: BLE001
                    return []

            # 候选人有 90+ 个，逐个读要 22 秒；并发读压到几秒
            with ThreadPoolExecutor(max_workers=8) as pool:
                for name, evs in zip(
                    [people[u] for u in people], pool.map(fetch_one, list(people))
                ):
                    for other in evs:
                        other_title = str(other.get("summary") or "")
                        other_day = _event_day(other)
                        if other_title and other_day:
                            copies.append((name, other_title, other_day))

        for event in pending:
            eid = str(event.get("id"))
            title = str(event.get("summary") or "")
            day = _event_day(event)
            best_score = 0.0
            best_name = ""
            if day:
                for owner, other_title, other_day in copies:
                    delta = abs((other_day - day).days)
                    if delta > 1:
                        continue
                    score = _title_similarity(title, other_title)
                    if delta == 1:
                        # 差一天降一档，避免把"德阳人民医院回访复盘会"
                        # 错配成"绵阳人民医院6s"
                        score -= 0.2
                    if score > best_score:
                        best_score = score
                        best_name = owner
            if best_score >= _TRIP_CREATOR_MIN_SCORE:
                resolved[eid] = (best_name, "copy")
                _TRIP_CREATOR_CACHE[eid] = {"name": best_name, "source": "copy", "at": time.monotonic()}
        # ④ 批次推断：同一次录入的一批行程，createTime 相差不超过 2 分钟，
        #    实测是同一个人连续录入的（10-12 10:24 那批=孟鹏杰、10-16 17:05
        #    那批=杨娜）。所以知道批里任意一条，就能推出整批。
        for event in pending:
            eid = str(event.get("id"))
            created = str(event.get("createTime") or "")
            if not created:
                continue
            try:
                base = datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                continue
            known = ""
            for other in events:
                oid = str(other.get("id"))
                if oid == eid:
                    continue
                other_created = str(other.get("createTime") or "")
                try:
                    delta = abs((base - datetime.strptime(other_created, "%Y-%m-%dT%H:%M:%SZ")).total_seconds())
                except ValueError:
                    continue
                if delta > 180:
                    continue
                if oid in resolved:
                    known = resolved[oid][0]
                    break
            if known:
                resolved[eid] = (known, "batch")
                _TRIP_CREATOR_CACHE[eid] = {"name": known, "source": "batch", "at": time.monotonic()}

            # 拿不到就**不显示**创建人，绝不猜。
            # 实测扫了 91 个可读日历、1517 条记录，26 条行程里只有 4 条能自动判定；
            # 剩下（如"眉山中6s调研与文化墙沟通"）任何人的主日历里都没有副本，
            # 只能靠设置页的「行程标题=创建人」对照表补。
        return resolved

    def _fetch_approval(self, user_id: str) -> list[MessageItem]:
        """审批实例：POST topapi/processinstance/list

        需权限 qyapi_aflow。没开通时钉钉会返回带申请链接的错误，
        原样透出给用户去申请。
        """
        resp = self._post_oapi(
            "topapi/processinstance/list",
            {"userId": user_id, "processCode": ""},
        )
        result = resp.get("result") or {}
        rows = result.get("list") if isinstance(result, dict) else result
        items = []
        for row in (rows or [])[:MAX_PER_KIND]:
            title = _first(row, "title", "subject", "form_name") or "钉钉审批"
            status = _first(row, "status", "result")
            items.append(
                MessageItem(
                    source=SOURCE_DINGTALK,
                    external_id=str(row.get("process_instance_id") or row.get("id") or ""),
                    title=title,
                    detail=f"钉钉审批 · {_approval_status_label(status)}",
                    due=format_due(row.get("create_time") or row.get("gmt_create")),
                    priority=PRIORITY_HIGH,
                    url=row.get("url") or "",
                    category="审批",
                    kind="approval",
                    creator=_first(row, "originator_name", "originator", "user_name") or "",
                    extra={"kind": "approval", "status": status},
                )
            )
        return items

    def _fetch_attendance(self, user_id: str) -> list[MessageItem]:
        """考勤结果：POST attendance/list

        注意路径**没有 topapi 前缀**（旧版网关的历史遗留）。
        需权限 qyapi_get_attendance_data。
        """
        today = datetime.now(timezone.utc).astimezone()
        work_from = (today - timedelta(days=6)).strftime("%Y-%m-%d 00:00:00")
        work_to = today.strftime("%Y-%m-%d 00:00:00")
        resp = self._post_oapi(
            "attendance/list",
            {"userIds": [user_id], "workDateFrom": work_from, "workDateTo": work_to},
        )
        result = resp.get("result") or {}
        rows = result.get("attendance_records") if isinstance(result, dict) else result
        items = []
        for row in (rows or [])[:MAX_PER_KIND]:
            work_date = _first(row, "work_date", "workDate")
            check_in = _first(row, "check_in_time", "checkInTime")
            check_out = _first(row, "check_out_time", "checkOutTime")
            items.append(
                MessageItem(
                    source=SOURCE_DINGTALK,
                    external_id=str(row.get("id") or f"{work_date}-{user_id}"),
                    title=f"考勤 · {_attendance_result_label(row)}",
                    detail=" · ".join(
                        p for p in [
                            f"上班 {check_in[11:16]}" if len(check_in) >= 16 else "",
                            f"下班 {check_out[11:16]}" if len(check_out) >= 16 else "",
                        ] if p
                    ) or "钉钉考勤",
                    due=format_due(work_date),
                    priority=PRIORITY_MEDIUM,
                    category="考勤",
                    kind="attendance",
                    extra={"kind": "attendance", "work_date": work_date},
                )
            )
        return items

    def _fetch_calendar(self, union_id: str) -> list[MessageItem]:
        """日程：GET /v1.0/calendar/users/{unionId}/calendars/primary/events

        响应顶层平铺：`{"events": [...], "requestId": "..."}`。
        时间区间用 ISO8601，钉钉不认 `+0800`，必须是 `+08:00` 或 `Z`。
        """
        if not union_id:
            return []
        # 与「消息提醒」用同一个窗口（近 7 天 + 未来 30 天）。
        # 之前这里是"今天起往后 30 天"，导致同源数据在两个板块里
        # 一个显示 0 条、一个显示 2 条，自相矛盾。
        time_min, time_max = _reminder_range()
        resp = self._get_v1(
            f"calendar/users/{union_id}/calendars/primary/events",
            {
                "maxResults": MAX_PER_KIND,
                "timeMin": time_min,
                "timeMax": time_max,
                "sortOrder": "startTime",
            },
        )
        events = resp.get("events") or []
        items = []
        for event in events[:MAX_PER_KIND]:
            item = _calendar_item(event, kind="calendar", category="个人日程", default_title="钉钉日程")
            if item is not None:
                items.append(item)
        return items

    # ---------- 消息提醒（带消息人与头像） ----------

    def _avatar_url(self, open_id: str) -> str:
        """取用户头像 URL；缺权限时返回空串。

        钉钉**没有公开的头像地址**（实测 static.dingtalk.com /
        static-legacy.dingtalk.com 的各种拼装路径全 404），唯一途径是
        `GET /v1.0/contact/users/{id}`，且需要 **Contact.User.Read** 权限。
        实测该接口返回 403 并带申请链接，说明接口存在、权限未开通。

        为避免每轮刷新都对每个人打一次注定 403 的请求，用类级缓存：
        - 权限整体不可用时，只探测一次，后续直接返回空
        - 单个用户的头像 URL 成功取到后缓存住
        """
        global _AVATAR_PERMISSION_DENIED

        if not open_id:
            return ""
        if open_id in _AVATAR_CACHE:
            return _AVATAR_CACHE[open_id]
        if _AVATAR_PERMISSION_DENIED:
            return ""

        try:
            with httpx.Client(timeout=8) as client:
                resp = client.get(
                    f"{API_V2}/contact/users/{open_id}",
                    headers={"x-acs-dingtalk-access-token": self._access_token()},
                )
        except httpx.HTTPError:
            return ""

        if resp.status_code == 403:
            # 权限没开：锁死，后续调用直接返回空（不重复打 403）
            _AVATAR_PERMISSION_DENIED = True
            self._kind_notes["remind"] = _AVATAR_PERMISSION_NOTE
            return ""
        if resp.status_code >= 400:
            return ""

        data = resp.json() if resp.text else {}
        url = str(data.get("avatarUrl") or data.get("avatar") or "").strip()
        if url:
            _AVATAR_CACHE[open_id] = url
        return url

    def fetch_reminders(self, user_id: str = "", limit: int = 20) -> list[dict[str, Any]]:
        """消息提醒：日程里的定时提醒，带「消息人」（组织者）与参与人。

        为什么只有日程：钉钉**没有开放收件箱查询接口**（实测工作通知
        `corpconversation/list`、DING `ding/task/list` 全部 404 / Invalid method），
        所以"我收到的聊天消息"取不到。能拿到"提醒"的只有日历事件的
        `reminders` 字段——实测每条日程都带 `[{"method":"dingtalk","minutes":"15"}]`，
        含义是"开始前 15 分钟用钉钉提醒我"。

        人员信息来自 `organizer`（组织者=消息人）与 `attendees[]`，
        两者都带 displayName + unionId。

        头像：钉钉没有公开的头像地址（实测 static.dingtalk.com /
        static-legacy.dingtalk.com 各路径均 404），必须走
        `contact/users/{id}` 且需要 Contact.User.Read 权限。
        所以这里只在权限可用时填 avatar_url，否则留空，
        由前端用「姓名首字 + 稳定配色」兜底——不阻塞功能。
        """
        effective_user = (user_id or "").strip() or self.configured_user_id()
        if not self.configured() or not effective_user:
            return []
        try:
            union_id = self.resolve_union_id(effective_user)
        except Exception as exc:  # noqa: BLE001
            self._errors.append(f"消息提醒：身份换算失败（{exc}）")
            return []

        resp = self._get_v1(
            f"calendar/users/{union_id}/calendars/primary/events",
            {
                "maxResults": max(1, int(limit)),
                "timeMin": _reminder_range()[0],
                "timeMax": _reminder_range()[1],
                "sortOrder": "startTime",
            },
        )
        reminders: list[dict[str, Any]] = []
        for event in (resp.get("events") or [])[: max(1, int(limit))]:
            start = _as_time_block(event.get("start") or event.get("startTime"))
            start_at = start.get("dateTime") or start.get("date") or ""
            if not start_at:
                continue

            organizer = event.get("organizer") or {}
            people: list[dict[str, str]] = []
            seen_ids: set[str] = set()
            if isinstance(organizer, dict) and organizer.get("displayName"):
                org_id = str(organizer.get("id") or "")
                people.append(
                    {
                        "name": str(organizer["displayName"]),
                        "id": org_id,
                        "role": "organizer",
                        "is_self": "1" if organizer.get("self") else "",
                    }
                )
                if org_id:
                    seen_ids.add(org_id)
            for att in event.get("attendees") or []:
                if not isinstance(att, dict):
                    continue
                name = str(att.get("displayName") or "")
                att_id = str(att.get("id") or "")
                # 钉钉会把组织者也列进 attendees，这里按 id 去重，
                # 否则同一个人的头像会出现两次（实测每条日程都这样）
                if not name or (att_id and att_id in seen_ids):
                    continue
                if att_id:
                    seen_ids.add(att_id)
                people.append(
                    {
                        "name": name,
                        "id": att_id,
                        "role": "attendee",
                        "is_self": "1" if att.get("self") else "",
                    }
                )
            if not people:
                continue

            # 给每个人补头像（缺权限时为空串，前端用姓名首字兜底）
            for person in people:
                person["avatar_url"] = self._avatar_url(person["id"])

            # 权限提示要在**每次**都写进 note。
            # 只在 _avatar_url 里首次遇到 403 时写的话，进程内缓存了
            # _AVATAR_PERMISSION_DENIED 之后就不会再写了，前端提示会消失。
            if _AVATAR_PERMISSION_DENIED:
                self._kind_notes["remind"] = _AVATAR_PERMISSION_NOTE

            # 每条日程可能配多个提醒（15 分钟前 / 5 分钟前），取最靠前的那个。
            # 全天日程钉钉会返回负数（如 -540 = 前一天 15:00），
            # 这种不换算成"提前 N 分钟"，交给前端只显示"已设提醒"。
            remind_minutes = ""
            methods: list[str] = []
            for rem in event.get("reminders") or []:
                if not isinstance(rem, dict):
                    continue
                m = str(rem.get("minutes") or "").strip()
                if m:
                    remind_minutes = m
                label = str(rem.get("method") or "").strip()
                if label and label not in methods:
                    methods.append(label)

            reminders.append(
                {
                    "id": str(event.get("id") or ""),
                    "title": _first(event, "summary", "subject", "title") or "钉钉日程提醒",
                    "start": start_at,
                    "all_day": bool(event.get("isAllDay")),
                    "remind_minutes": remind_minutes,
                    "remind_methods": methods,
                    "status": str(event.get("status") or ""),
                    "people": people,
                }
            )
        return reminders


# ---------- 解析辅助 ----------


def _calendar_item(
    event: dict[str, Any],
    *,
    kind: str,
    category: str,
    default_title: str,
    creator: str | None = None,
    show_creator: bool = True,
) -> MessageItem | None:
    """把一条钉钉日程事件转成 MessageItem。

    creator：显式指定这条记录的"创建人"。部门行程要展示的是**这条行程的创建人**
    （钉钉"动态"里"XX 新建了日程"的那个人），和每条行程的组织者（organizer）
    不是一回事，所以允许外部覆盖；不传就用工单里的 organizer。

    show_creator：是否把"创建人 XXX"写进 detail 文本。部门行程要把它渲染成
    头像（见前端），所以那里传 False。

    全天日程的 start/end 用 `date` 字段（"2026-09-07"），
    定时日程用 `dateTime`（"2026-10-21T15:00:00+08:00"），两种都要认。
    """
    start = _as_time_block(event.get("start") or event.get("startTime") or event.get("start_time"))
    end = _as_time_block(event.get("end") or event.get("endTime") or event.get("end_time"))
    start_at = start.get("dateTime") or start.get("date") or ""
    if not start_at:
        return None
    organizer = event.get("organizer") or {}
    if isinstance(organizer, dict):
        organizer_name = str(organizer.get("displayName") or organizer.get("id") or "")
    else:
        organizer_name = str(organizer)
    # creator 显式传 None 才回落到 organizer；传空串表示"确定没有创建人"
    creator_name = organizer_name if creator is None else creator
    all_day = bool(event.get("isAllDay"))
    return MessageItem(
        source=SOURCE_DINGTALK,
        external_id=str(event.get("id") or ""),
        title=_first(event, "summary", "subject", "title") or default_title,
        detail=_calendar_detail(event, end, creator_name, all_day, show_creator=show_creator),
        due=format_due(start_at, all_day=all_day),
        priority=PRIORITY_MEDIUM,
        category=category,
        kind=kind,
        creator=creator_name,
        # created_at = 钉钉"动态"里那条"XX 新建了日程"的时间戳。
        # 实测与事件详情页动态里的时间完全一致（分秒不差），
        # 前端把它显示出来，这一行就和钉钉的动态对得上了。
        extra={
            "kind": kind,
            "start": start_at,
            "all_day": all_day,
            **({"created_at": str(event.get("createTime"))} if event.get("createTime") else {}),
        },
    )


def _approval_status_label(status: str) -> str:
    return {
        "RUNNING": "审批中",
        "COMPLETED": "已完成",
        "TERMINATED": "已撤销",
        "agree": "已同意",
        "refuse": "已拒绝",
    }.get(str(status or "").strip(), str(status or "状态未知"))


def _attendance_result_label(row: dict[str, Any]) -> str:
    """考勤结果的多种表示都归一成中文短标签。"""
    for key in ("result", "attendance_result", "is_exception", "exception_type"):
        value = row.get(key)
        if value in (None, ""):
            continue
        text = str(value).strip()
        if text.lower() in ("true", "1"):
            return "异常"
        if text.lower() in ("false", "0"):
            return "正常"
        return {
            "Normal": "正常",
            "Late": "迟到",
            "Early": "早退",
            "SeriousLate": "严重迟到",
            "Absent": "缺卡",
            "Leave": "请假",
            "OffDuty": "休息",
        }.get(text, text)
    return "考勤记录"


def _unwrap(payload: dict[str, Any], path: str) -> dict[str, Any]:
    """钉钉两套网关的错误格式不同，统一在这里拦一次。

    - 旧版：{"errcode": 88, "errmsg": "..."}
    - v1.0：{"code": "InvalidParameter.xxx", "message": "..."}
    """
    errcode = payload.get("errcode", payload.get("code"))
    if errcode in (None, 0, "0", "Ok", "ok"):
        return payload
    message = payload.get("errmsg") or payload.get("message") or errcode
    if isinstance(errcode, str) and errcode and errcode not in str(message):
        message = f"{message}（{errcode}）"
    raise RuntimeError(f"{path} 返回错误：{message}")


def _first(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if value not in (None, "", [], {}):
            return str(value)
    return ""


def _as_time_block(value: Any) -> dict[str, Any]:
    """日程的 start/end 可能是 {dateTime,timeZone} 结构，也可能是裸时间字符串。"""
    if isinstance(value, dict):
        return value
    if value in (None, ""):
        return {}
    return {"dateTime": str(value)}


def _calendar_range() -> tuple[str, str]:
    """日程查询区间：今天 00:00 起，往后 30 天。

    钉钉要求 `+08:00` 或 `Z`，Python 的 `%z` 会输出 `+0800` 被拒。
    """
    start = datetime.now(timezone.utc).astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=_CALENDAR_WINDOW_DAYS)
    return start.isoformat(), end.isoformat()


def _reminder_range() -> tuple[str, str]:
    """个人日程的查询区间：**今天 → 本周日结束**（下一个周一 00:00 之前）。

    之前是"近 7 天 + 未来 30 天"，会把 10-16、10-19 那种几天前的日程
    一直挂在首页上。按需求改成只看周一到周天这一周：
    周一打开就是 10-26 ~ 11-01；周中打开则只显示今天到周日的剩余部分
    （已经过去的那几天不再回头显示）。

    部门行程用另一个窗口（_trip_range：往前 14 天 → 明天，因为要按
    "覆盖今天"筛跨天行程），两者互不影响。
    """
    now = datetime.now(timezone.utc).astimezone()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # 本周一（weekday(): 周一=0）→ 下一个周一 = 本周日结束
    this_monday = today - timedelta(days=today.weekday())
    next_monday = this_monday + timedelta(days=7)
    return today.isoformat(), next_monday.isoformat()


def _trip_range() -> tuple[str, str]:
    """部门行程的查询区间：**今天往前 14 天 → 明天 00:00**。

    两个坑，都是实测踩出来的：

    1. 钉钉的日程接口按**开始时间**过滤（只返回 timeMin 之后开始的事件）。
       跨天行程如果前几天就开始、今天还没结束，直接查"今天 00:00 → 明天"
       会**一条都取不到**（实测返回 0 条）。所以先用宽松窗口把跨天行程捞回来，
       再在本地按"覆盖今天"筛（见 _event_covers_day）。
    2. 窗口也不能太宽：接口按开始时间升序返回，maxResults 会截断**靠后**的，
       窗口太宽反而把最近的行程挤掉。14 天足够覆盖连续多天的行程。

    只展示"覆盖今天"是为了和钉钉月视图一致——用户在钉钉里看今天那一格，
    首页就应该显示同样那几条。
    """
    now = datetime.now(timezone.utc).astimezone()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        (today - timedelta(days=_TRIP_LOOKBACK_DAYS)).isoformat(),
        (today + timedelta(days=1)).isoformat(),
    )


def _event_covers_day(event: dict[str, Any], day_start: datetime) -> bool:
    """事件是否"覆盖"某一天（本地时区）。

    钉钉的月视图会把跨天日程画在它覆盖的**每一天**上，所以"今天的行程"
    不只是"今天开始"的，还包括前几天开始、今天仍未结束的行程
    （例：10-20 09:00 → 10-23 17:30 的调研，10-23 当天它还在进行中）。

    按半开区间 [start, end) 判断：
    - 全天日程（只有 date）：``start.date <= 当天 < end.date``（end 是排他的）
    - 定时日程（dateTime）：``start < 当天结束 且 end > 当天开始``
    """
    start = _as_time_block(event.get("start") or event.get("startTime") or event.get("start_time"))
    end = _as_time_block(event.get("end") or event.get("endTime") or event.get("end_time"))
    s_raw = str(start.get("dateTime") or start.get("date") or "")
    e_raw = str(end.get("dateTime") or end.get("date") or "")
    if not s_raw:
        return False

    day_end = day_start + timedelta(days=1)
    if start.get("date") and not start.get("dateTime"):
        # 全天日程：只有日期，按天比较；end 是排他的
        target = day_start.date().isoformat()
        e_date = e_raw[:10] if e_raw else target
        return s_raw[:10] <= target < e_date

    s_dt = _parse_iso(s_raw)
    if s_dt is None:
        return False
    e_dt = _parse_iso(e_raw) or s_dt
    return s_dt < day_end and e_dt > day_start


def _trip_due_label(event: dict[str, Any], day_start: datetime) -> str:
    """部门行程的日期标签：以"今天"为基准。

    部门行程只展示"覆盖今天"的行程，但事件本身可能前几天才开始。
    若沿用 format_due 的开始日，跨天行程会显示成"昨天 全天"，
    在"今天的行程"列表里会让人以为不是今天的。
    """
    end = _as_time_block(event.get("end") or event.get("endTime") or event.get("end_time"))
    end_raw = str(end.get("dateTime") or end.get("date") or "")
    if not end.get("dateTime"):
        return "今天 全天"
    end_dt = _parse_iso(end_raw)
    if end_dt is None:
        return "今天"
    end_local = end_dt.astimezone()
    if end_local.date() == day_start.date():
        return f"今天 至 {end_local:%H:%M}"
    return f"今天 至 {end_local:%m/%d %H:%M}"


def _event_start_dt(event: dict[str, Any]) -> datetime | None:
    """事件的开始时间（本地时区），用于排序。"""
    start = _as_time_block(event.get("start") or event.get("startTime") or event.get("start_time"))
    raw = str(start.get("dateTime") or start.get("date") or "")
    return _parse_iso(raw)


def _parse_iso(value: str) -> datetime | None:
    """解析钉钉的 ISO8601 时间串（带 +08:00 偏移），失败返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        # 没带时区的按本地时间理解
        moment = moment.astimezone()
    return moment


def _day_start_iso(day: date) -> str:
    """日期 → 当天 00:00 的 ISO8601（带本地时区）。

    钉钉的接口参数只接受完整 ISO8601：纯日期（"2026-10-14"）会被拒。
    """
    tz = datetime.now(timezone.utc).astimezone().tzinfo
    return datetime(day.year, day.month, day.day, tzinfo=tz).isoformat()


def _people_of_event(event: dict[str, Any]) -> dict[str, str]:
    """取一条日程里出现的人：组织者 + 参与者（unionId → 姓名）。"""
    people: dict[str, str] = {}
    organizer = event.get("organizer") or {}
    if isinstance(organizer, dict) and organizer.get("displayName") and organizer.get("id"):
        people[str(organizer["id"])] = str(organizer["displayName"])
    for att in event.get("attendees") or []:
        if isinstance(att, dict) and att.get("displayName") and att.get("id"):
            people[str(att["id"])] = str(att["displayName"])
    return people


def _title_similarity(a: str, b: str) -> float:
    """两条日程标题的相似度（0~1）。

    用来把"绵阳市人民医院回访"和"绵阳人民运营回访"认成同一条行程的副本。
    """
    na = re.sub(r"[\s\-—_·、,，.。:：()（）]", "", a or "")
    nb = re.sub(r"[\s\-—_·、,，.。:：()（）]", "", b or "")
    if not na or not nb:
        return 0.0
    if na in nb or nb in na:
        return 1.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def _event_day(event: dict[str, Any]) -> date | None:
    """事件开始的那一天（本地时区）。"""
    start = _as_time_block(event.get("start") or event.get("startTime") or event.get("start_time"))
    raw = str(start.get("dateTime") or start.get("date") or "")[:10]
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def _surname(name: str) -> str:
    """取姓氏用于展示（创建人、头像）。

    复姓（欧阳/司马…）要取两个字，其余取第一个字。
    中文姓名习惯用姓氏称呼（"邱"而不是"邱宇"），首页展示更紧凑，
    和头像里的那个字也保持一致。
    """
    text = str(name or "").strip()
    if not text:
        return ""
    if len(text) >= 2 and text[:2] in _COMPOUND_SURNAMES:
        return text[:2]
    return text[:1]


def _calendar_detail(
    event: dict[str, Any],
    end: dict[str, Any],
    creator: str,
    all_day: bool = False,
    show_creator: bool = True,
) -> str:
    place = event.get("location") or event.get("onlineMeetingInfo") or ""
    if isinstance(place, dict):
        place = place.get("address") or place.get("meetingUrl") or ""
    end_at = end.get("dateTime") or end.get("date") or ""
    bits = [
        # 这里显示的是这条记录的"创建人"，不是"参与人"——原来的标注不准确。
        f"创建人 {_surname(creator)}" if (creator and show_creator) else "",
        "全天" if all_day else (f"至 {end_at[11:16]}" if len(str(end_at)) >= 16 else ""),
        str(place) if place else "",
    ]
    return " · ".join([b for b in bits if b]) or "钉钉日程"
