"""探测 Salesforce org 里三个首页板块能否取到数据。

目标板块：
  1. 今天的任务   (today's tasks)        —— 标准对象 Task，ActivityDate = TODAY
  2. 要批准的项目 (projects to approve)  —— 审批对象 ProcessInstanceWorkitem / ProcessInstance
  3. 销售周计划   (sales weekly plan)    —— 自定义对象（__c），按命名猜候选再逐一试

用法：
  先在前端「Salesforce 登录」里成功登录（后端会把 sid 写进 integrations.json），
  再运行本脚本。脚本复用已存的 sid，不需要再填 6 位动态码。

  python tools/probe_salesforce_schema.py
"""
from __future__ import annotations

import os
import sys
import json
import textwrap
from pathlib import Path

# 这台机的代理环境变量会让 httpx 访问 Salesforce 直接 502，先清掉。
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

HERE = Path(__file__).resolve()
BACKEND_DIR = HERE.parents[1] / "backend"   # platform/tools -> platform -> backend
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.salesforce_messages import SalesforceAdapter  # noqa: E402
from app.services import integration_config  # noqa: E402


def color(text: str, ok: bool) -> str:
    mark = "✅" if ok else "⚠️ "
    return f"{mark} {text}"


def main() -> int:
    cfg = integration_config.load_config()["salesforce"]
    print("=" * 64)
    print("Salesforce 首页板块可获取性探测")
    print("=" * 64)
    print(f"实例 : {cfg.get('instance_url')}")
    print(f"账号 : {cfg.get('username')}")
    print(f"模式 : {cfg.get('auth_mode')}")
    has_sid = bool(str(cfg.get("access_token") or "").strip())
    print(f"已存会话 sid : {'有' if has_sid else '无（请先在前端登录）'}")

    adapter = SalesforceAdapter()

    # 1) 暖会话：复用已存 sid；若没有则报缺登录
    try:
        adapter._ensure_session()
    except Exception as exc:  # noqa: BLE001
        print()
        print(color(f"无法建立会话：{exc}", False))
        print("→ 请先在前端 Salesforce 登录框输入账号 + 密码 + 腾讯身份验证器 6 位码登录，")
        print("  成功后再运行本脚本。")
        return 2

    base = adapter._base()
    ver = adapter._api_version()
    headers = adapter._headers()
    client = adapter._client

    report: dict[str, object] = {"instance": base, "sections": {}}

    # 2) 列出全部 SObject（全局 describe），筛出与审批/计划相关的候选
    print()
    print("-" * 64)
    print("全局 SObject 列表（筛选用）")
    print("-" * 64)
    sobj_resp = client.get(f"{base}/services/data/{ver}/sobjects", headers=headers)
    sobjects: list[dict] = []
    if sobj_resp.status_code >= 400:
        print(color(f"列举 SObject 失败（{sobj_resp.status_code}）：{sobj_resp.text[:160]}", False))
    else:
        sobjects = list(sobj_resp.json().get("sobjects") or [])
        names = [s.get("name") for s in sobjects]
        print(f"共 {len(names)} 个对象。")

    def find_candidates(keywords: list[str]) -> list[str]:
        kw = [k.lower() for k in keywords]
        hits = [
            n for n in names
            if any(k in n.lower() for k in kw)
        ]
        return hits

    approval_candidates = find_candidates(["approval", "processinstance", "workitem", "审批"])
    plan_candidates = find_candidates(["plan", "week", "周", "sales", "销售", "schedule", "计划"])
    print("审批相关对象候选:", approval_candidates or "（无精确匹配）")
    print("计划相关对象候选:", plan_candidates or "（无精确匹配）")

    def count_query(soql: str) -> tuple[int, str]:
        """返回 (数量, 错误信息)。"""
        try:
            recs = adapter._query(soql)
            return len(recs), ""
        except Exception as exc:  # noqa: BLE001
            return -1, str(exc)

    # 3) 板块一：今天的任务
    print()
    print("-" * 64)
    print("板块一：今天的任务（Task WHERE ActivityDate = TODAY）")
    print("-" * 64)
    task_count, task_err = count_query(
        "SELECT COUNT() FROM Task WHERE ActivityDate = TODAY"
    )
    if task_err:
        print(color(f"查询失败：{task_err}", False))
        task_ok = False
    else:
        print(color(f"今天到期/今天的 Task 数量 = {task_count}", True))
        task_ok = True
    report["sections"]["today_tasks"] = {  # type: ignore[index]
        "sobject": "Task",
        "fetchable": task_ok,
        "count": task_count,
        "error": task_err,
    }

    # 4) 板块二：要批准的项目（审批工作项）
    print()
    print("-" * 64)
    print("板块二：要批准的项目（审批工作项）")
    print("-" * 64)
    approval_queries = {
        "ProcessInstanceWorkitem": "SELECT COUNT() FROM ProcessInstanceWorkitem",
        "ProcessInstance(pending)": "SELECT COUNT() FROM ProcessInstance WHERE Status = 'Pending'",
    }
    best_approval = None
    for label, soql in approval_queries.items():
        cnt, err = count_query(soql)
        if err:
            print(f"  · {label}: 失败 —— {err[:120]}")
        else:
            print(color(f"  · {label}: 数量 = {cnt}", True))
            if best_approval is None and cnt > 0:
                best_approval = label
    # 若上面都拿不到，尝试常用审批对象名是否存在
    approval_ok = best_approval is not None
    if not approval_ok:
        for cand in approval_candidates:
            cnt, err = count_query(f"SELECT COUNT() FROM {cand}")
            if not err and cnt >= 0:
                print(f"  · 候选 {cand}: 数量 = {cnt}")
                if cnt > 0:
                    best_approval = cand
                    approval_ok = True
    report["sections"]["approvals"] = {  # type: ignore[index]
        "fetchable": approval_ok,
        "best_object": best_approval,
        "candidates": approval_candidates,
    }

    # 5) 板块三：销售周计划（自定义对象）
    print()
    print("-" * 64)
    print("板块三：销售周计划（自定义对象探测）")
    print("-" * 64)
    plan_ok = False
    best_plan = None
    # 优先按候选命名试，再退化为扫所有 __c 对象
    probe_plan_objects = list(plan_candidates)
    if not probe_plan_objects:
        probe_plan_objects = [n for n in names if n.endswith("__c")][:30]
    for obj in probe_plan_objects:
        cnt, err = count_query(f"SELECT COUNT() FROM {obj}")
        if err:
            # 有些名字是前缀匹配到的非对象，忽略
            continue
        print(f"  · {obj}: 数量 = {cnt}")
        if cnt > 0 and best_plan is None:
            best_plan = obj
            plan_ok = True
    if not plan_ok:
        print("  （未在候选命名里找到有数据的计划对象，可能需要人工确认对象 API 名）")
    report["sections"]["sales_weekly_plan"] = {  # type: ignore[index]
        "fetchable": plan_ok,
        "best_object": best_plan,
        "candidates": plan_candidates,
    }

    # 6) 汇总
    print()
    print("=" * 64)
    print("汇总")
    print("=" * 64)
    print("今天的任务  :", "可获取" if report["sections"]["today_tasks"]["fetchable"] else "暂不可获取")  # type: ignore[index]
    print("要批准的项目:", ("可获取 · " + str(report["sections"]["approvals"]["best_object"])) if report["sections"]["approvals"]["fetchable"] else "暂不可获取")  # type: ignore[index]
    print("销售周计划  :", ("可获取 · " + str(report["sections"]["sales_weekly_plan"]["best_object"])) if report["sections"]["sales_weekly_plan"]["fetchable"] else "暂不可获取（需确认对象名）")  # type: ignore[index]

    out = HERE.parent.parent / "logs" / "salesforce_probe.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已写入：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
