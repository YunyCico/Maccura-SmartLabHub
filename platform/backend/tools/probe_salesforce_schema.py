"""Salesforce 元数据探查脚本（开发期一次性工具）。

目的：在 SmartLabHub 已保存 Salesforce 凭据（首页登录后写入 integrations.json）
的前提下，连上 org 探明三块新看板对应的对象与字段是否可取：

  1. 今天的任务   -> 标准对象 Task（ActivityDate = TODAY）
  2. 要批准的项目 -> 待审批项，候选 ProcessInstanceWorkitem / ApprovalRequest / 自定义对象
  3. 销售周计划   -> 极大概率是自定义对象（Weekly_Plan__c / 周计划__c 等）

用法：
  1. 先在前端首页 Salesforce 快速登录卡片填入 用户名 + 密码 + TOTP 密钥 并登录，
     凭据与 sid 会写入 platform/backend/data/integrations.json。
  2. 在本机（已激活后端 venv）运行：
       .venv/Scripts/python.exe tools/probe_salesforce_schema.py
  3. 脚本输出每一块的探查结论（可取 / 不可取 / 候选对象与字段）。

注意：这是开发期探针，不进入任何请求链路，用完可删。
"""

from __future__ import annotations

import os
import sys
import textwrap

# 让脚本能 import app.*
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

from app.services import integration_config  # noqa: E402
from app.services.salesforce_messages import SalesforceAdapter  # noqa: E402


def _section(title: str) -> None:
    print("\n" + "=" * 64)
    print(title)
    print("=" * 64)


def probe_global_objects(adapter: SalesforceAdapter) -> list[dict]:
    """列出 org 全部 SObject，返回 (ApiName, Label) 列表。"""
    base = adapter._base()
    ver = adapter._api_version()
    resp = adapter._client.get(
        f"{base}/services/data/{ver}/sobjects",
        headers=adapter._headers(),
    )
    resp.raise_for_status()
    sobjects = resp.json().get("sobjects", [])
    return [
        {"name": s.get("name"), "label": s.get("label"), "custom": s.get("custom", False)}
        for s in sobjects
    ]


def try_query(adapter: SalesforceAdapter, soql: str) -> dict:
    """跑一条 SOQL，返回探查结果。"""
    try:
        records = adapter._query(soql)
        sample = records[0] if records else {}
        fields = sorted(sample.keys()) if isinstance(sample, dict) else []
        return {
            "ok": True,
            "count": len(records),
            "fields": fields,
            "sample": sample,
            "error": "",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "count": 0, "fields": [], "sample": {}, "error": str(exc)}


def main() -> None:
    settings = integration_config.effective_settings()["salesforce"]
    adapter = SalesforceAdapter()
    adapter._settings = settings

    if not adapter.configured():
        _section("前置检查失败")
        print("Salesforce 尚未配置凭据（用户名 / 密码 / TOTP 密钥 / 会话）。")
        print("请先在前端首页 Salesforce 快速登录卡片完成登录，再运行本脚本。")
        print("当前原因：", adapter._unconfigured_reason())
        return

    _section("1. 建立会话")
    try:
        adapter._ensure_session()
        sid = adapter._client.cookies.get("sid") or settings.get("access_token")
        print("会话建立成功，sid 尾 4 位：", (sid or "")[-4:] or "(无)")
    except Exception as exc:  # noqa: BLE001
        print("登录失败：", exc)
        return

    _section("2. 列出全部 SObject（用于定位自定义对象）")
    try:
        sobjects = probe_global_objects(adapter)
    except Exception as exc:  # noqa: BLE001
        print("获取 SObject 列表失败：", exc)
        return
    print(f"org 共有 {len(sobjects)} 个对象。")

    # 销售周计划：在全部对象里找名字/标签含 周/计划/Plan/Weekly 的
    plan_keywords = ("周计划", "周", "计划", "plan", "weekly", "salesplan", "销售")
    plan_candidates = [
        s for s in sobjects
        if any(k in (str(s["label"]) + str(s["name"])).lower() for k in plan_keywords)
    ]
    print("\n[销售周计划] 候选对象（名称/标签含 周/计划/Plan 等）：")
    for s in plan_candidates[:25]:
        tag = "自定义" if s["custom"] else "标准"
        print(f"  - {s['name']}  ({s['label']})  [{tag}]")

    project_keywords = ("项目", "project", "approval", "审批", "申请")
    project_candidates = [
        s for s in sobjects
        if any(k in (str(s["label"]) + str(s["name"])).lower() for k in project_keywords)
    ]
    print("\n[要批准的项目] 候选对象（名称/标签含 项目/审批/Approval 等）：")
    for s in project_candidates[:25]:
        tag = "自定义" if s["custom"] else "标准"
        print(f"  - {s['name']}  ({s['label']})  [{tag}]")

    _section("3. 逐一尝试三块数据的查询")
    # (a) 今天的任务：标准 Task
    today_sql = (
        "SELECT Id, Subject, ActivityDate, Status, Priority, "
        "What.Name, Who.Name, Owner.Name FROM Task "
        "WHERE ActivityDate = TODAY AND IsClosed = false "
        "ORDER BY ActivityDate ASC LIMIT 25"
    )
    print("\n[今天的任务] SOQL:\n" + textwrap.indent(today_sql, "  "))
    r = try_query(adapter, today_sql)
    print("  结果：", "可取" if r["ok"] else "失败", f"({r['count']} 条)" if r["ok"] else r["error"])
    if r["ok"]:
        print("  可用字段：", ", ".join(r["fields"]))

    # (b) 要批准的项目：多个候选
    approval_sqls = {
        "ProcessInstanceWorkitem": (
            "SELECT Id, TargetObject.Name, ProcessInstance.Status, "
            "Actor.Name, CreatedDate FROM ProcessInstanceWorkitem "
            "WHERE IsDeleted = false LIMIT 25"
        ),
        "ApprovalRequest": (
            "SELECT Id, Record.Name, Status, SubmittedBy.Name, "
            "CreatedDate FROM ApprovalRequest LIMIT 25"
        ),
    }
    print("\n[要批准的项目] 候选 SOQL：")
    for obj, sql in approval_sqls.items():
        print(f"  -- {obj} --\n" + textwrap.indent(sql, "     "))
        r = try_query(adapter, sql)
        print(f"  结果：{('可取' if r['ok'] else '失败')} "
              f"({r['count']} 条)" if r["ok"] else f"  结果：失败 {r['error']}")
        if r["ok"]:
            print("  可用字段：", ", ".join(r["fields"]))

    # (c) 销售周计划：对候选自定义对象各试一条
    print("\n[销售周计划] 对候选自定义对象尝试 SELECT：")
    tried = 0
    for s in plan_candidates:
        name = s["name"]
        if not name.endswith("__c") and not s["custom"]:
            continue
        sql = f"SELECT Id, Name FROM {name} LIMIT 5"
        r = try_query(adapter, sql)
        print(f"  {name}: {'可取' if r['ok'] else '失败'} "
              f"({r['count']} 条)" if r["ok"] else f"  {name}: 失败 {r['error']}")
        if r["ok"] and tried < 3:
            # 取一条做 describe 看字段
            try:
                desc = adapter._client.get(
                    f"{adapter._base()}/services/data/{adapter._api_version()}/sobjects/{name}/describe",
                    headers=adapter._headers(),
                ).json()
                fields = [f.get("name") for f in desc.get("fields", [])][:40]
                print(f"     {name} 字段：{', '.join(fields)}")
            except Exception as exc:  # noqa: BLE001
                print(f"     {name} describe 失败：{exc}")
            tried += 1

    _section("4. 结论")
    print("以上结果即为三块看板能否取数的依据。把输出贴回给 SmartLabHub 开发，")
    print("即可据此确定 SOQL 与字段映射，进而调整首页 Salesforce 区域 UI。")


if __name__ == "__main__":
    main()
