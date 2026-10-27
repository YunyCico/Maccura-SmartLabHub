"""诊断：针对当前登录用户，确认三个首页板块的真实查询与数据量。

修复 probe 的 COUNT 误区：SELECT COUNT() 的计数在 totalSize 字段，
records 恒为空列表，所以之前 len(records) 永远 0。这里统一读 totalSize。
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta
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


def main() -> int:
    cfg = integration_config.load_config()["salesforce"]
    adapter = SalesforceAdapter()
    adapter._ensure_session()
    base = adapter._base()
    ver = adapter._api_version()
    headers = adapter._headers()
    client = adapter._client

    def query(soql: str):
        url = f"{base}/services/data/{ver}/query"
        r = client.get(url, params={"q": soql}, headers=headers)
        if r.status_code >= 400:
            return None, None, f"{r.status_code}: {r.text[:300]}"
        data = r.json()
        return data.get("totalSize", len(data.get("records", []))), data.get("records", []), None

    # 当前用户
    _, me_recs, _ = query("SELECT Id, Name, Username FROM User WHERE Username = '%s'" % cfg.get("username"))
    me = me_recs[0] if me_recs else {}
    me_id = me.get("Id", "")
    print("当前用户 :", me.get("Name"), me_id)

    today = date.today().isoformat()
    week_start = (date.today() - timedelta(days=date.today().weekday())).isoformat()
    print("today =", today, " week_start =", week_start)

    def block(title, soqls):
        print("\n" + "=" * 64)
        print(title)
        print("=" * 64)
        for label, soql in soqls:
            total, recs, err = query(soql)
            if err:
                print(f"  [ERR] {label}: {err}")
                continue
            print(f"  {label}: totalSize={total}")
            for r in (recs or [])[:4]:
                print("      -", r)

    block("板块一：今天的任务", [
        ("TODAY 我名下", f"SELECT Id, Subject, ActivityDate, Status, Priority, What.Name FROM Task WHERE ActivityDate = {{today}} AND OwnerId = '{me_id}'".format(today=today)),
        ("TODAY 全部", f"SELECT Id, Subject, ActivityDate, Status, OwnerId FROM Task WHERE ActivityDate = {{today}}".format(today=today)),
        ("本周(我)", f"SELECT Id, Subject, ActivityDate, Status FROM Task WHERE ActivityDate >= {{ws}} AND OwnerId = '{me_id}' ORDER BY ActivityDate ASC".format(ws=week_start)),
        ("未完成且>=今天(我)", f"SELECT Id, Subject, ActivityDate, Status FROM Task WHERE IsClosed=false AND ActivityDate >= {{today}} AND OwnerId = '{me_id}' ORDER BY ActivityDate ASC".format(today=today)),
    ])

    block("板块二：要批准的项目", [
        ("Workitem Actor=我", f"SELECT Id, ProcessInstanceId, ActorId, CreatedDate, ProcessInstance.TargetObjectId, ProcessInstance.TargetObject.Name, ProcessInstance.TargetObject.Type FROM ProcessInstanceWorkitem WHERE ActorId = '{me_id}' ORDER BY CreatedDate DESC LIMIT 10"),
        ("Workitem 全部最近", "SELECT Id, ActorId, CreatedDate, ProcessInstance.TargetObject.Name, ProcessInstance.TargetObject.Type FROM ProcessInstanceWorkitem ORDER BY CreatedDate DESC LIMIT 10"),
    ])

    block("板块三：销售周计划", [
        ("Weekly_Plan Owner=我", f"SELECT Id, Name, WeekName__c, Start_Date__c, End_Date__c, Status__c, Company__c, Manager__c, Business_Total__c, CreatedDate FROM Weekly_Plan__c WHERE OwnerId = '{me_id}' ORDER BY CreatedDate DESC LIMIT 10"),
        ("Weekly_Plan 全部最近", "SELECT Id, Name, WeekName__c, Start_Date__c, End_Date__c, Status__c, Company__c, Manager__c, CreatedDate FROM Weekly_Plan__c ORDER BY CreatedDate DESC LIMIT 10"),
    ])
    return 0


if __name__ == "__main__":
    sys.exit(main())
