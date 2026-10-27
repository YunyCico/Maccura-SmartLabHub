"""诊断：确认 Salesforce 连接能取到真实数据，并摸清三个板块的正确对象/字段。

复用已存的 sid（前端登录后写入 integrations.json）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

HERE = Path(__file__).resolve()
BACKEND_DIR = HERE.parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.salesforce_messages import SalesforceAdapter  # noqa: E402
from app.services import integration_config  # noqa: E402


def show(title: str) -> None:
    print("\n" + "=" * 64)
    print(title)
    print("=" * 64)


def main() -> int:
    cfg = integration_config.load_config()["salesforce"]
    adapter = SalesforceAdapter()
    adapter._ensure_session()
    base = adapter._base()
    ver = adapter._api_version()
    headers = adapter._headers()
    client = adapter._client

    def get(soql: str, limit: int = 5):
        url = f"{base}/services/data/{ver}/query"
        r = client.get(url, params={"q": soql}, headers=headers)
        if r.status_code >= 400:
            return None, f"{r.status_code}: {r.text[:300]}"
        data = r.json()
        return data.get("records", []), None

    # 0) 当前登录用户
    show("0) 当前登录用户")
    recs, err = get("SELECT Id, Name, Username, Profile.Name FROM User WHERE Username = '%s' LIMIT 1" % cfg.get("username"))
    me = recs[0] if recs else None
    if err:
        print("ERR", err)
    else:
        print("me =", me)
    me_id = me.get("Id") if me else ""

    # 1) 任务：总数 / 我名下的 / 最近的
    show("1) Task 真实数据核对")
    for label, soql in [
        ("Task 总数(无过滤)", "SELECT COUNT() FROM Task"),
        ("Task 我名下", f"SELECT COUNT() FROM Task WHERE OwnerId = '{me_id}'"),
    ]:
        recs, err = get(soql)
        print(f"  {label}: {recs if err is None else err}")
    recs, err = get("SELECT Id, Subject, ActivityDate, Status, Priority, OwnerId FROM Task ORDER BY ActivityDate DESC NULLS LAST LIMIT 8")
    print("  最近 Task 样本:")
    for r in (recs or []):
        print("   -", {k: r.get(k) for k in ("Subject", "ActivityDate", "Status", "Priority")})

    # 2) 审批：工作项 / 流程实例 全部状态 + 样本
    show("2) 审批对象核对")
    for label, soql in [
        ("ProcessInstanceWorkitem 总数", "SELECT COUNT() FROM ProcessInstanceWorkitem"),
        ("ProcessInstance 总数", "SELECT COUNT() FROM ProcessInstance"),
        ("ProcessInstance Pending", "SELECT COUNT() FROM ProcessInstance WHERE Status = 'Pending'"),
    ]:
        recs, err = get(soql)
        print(f"  {label}: {recs if err is None else err}")
    recs, err = get("SELECT Id, ProcessInstanceId, ActorId, CreatedDate FROM ProcessInstanceWorkitem ORDER BY CreatedDate DESC LIMIT 5")
    print("  ProcessInstanceWorkitem 样本:", recs if err is None else err)

    # 3) 周计划：重点对象 + 字段描述
    show("3) 周计划对象核对")
    plan_objs = ["Weekly_Plan__c", "Weekly_PlanDetails__c", "SalesMonthPlan__c", "PurchaseDemandPlan__c"]
    for obj in plan_objs:
        recs, err = get(f"SELECT COUNT() FROM {obj}")
        print(f"  {obj} 总数: {recs if err is None else err}")
    recs, err = get("SELECT Id, Name FROM Weekly_Plan__c ORDER BY CreatedDate DESC LIMIT 5")
    print("  Weekly_Plan__c 样本:", recs if err is None else err)
    # 描述 Weekly_Plan__c 字段，便于后续选字段
    desc = client.get(f"{base}/services/data/{ver}/sobjects/Weekly_Plan__c/describe", headers=headers)
    if desc.status_code < 400:
        fields = [f["name"] for f in desc.json().get("fields", [])]
        print("  Weekly_Plan__c 字段:", fields[:60])
    else:
        print("  describe 失败:", desc.status_code, desc.text[:200])

    return 0


if __name__ == "__main__":
    sys.exit(main())
