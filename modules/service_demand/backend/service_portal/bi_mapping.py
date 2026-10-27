"""BI 看板字段映射配置（可由用户用 Excel 覆盖）。

设计目标
--------
业务方不需要改代码，只需要编辑一张 Excel（`BI字段对应表.xlsx`），
后端每次构建看板数据集时读取该表，按表里的配置填报表。

表结构（3 个工作表）
--------------------
1. 「字段对应表」—— 业务方主要修改的地方
     看板维度 | 看板表头 | 取数字段 | 字段中文名 | 取数方式 | 备注
   - 「看板维度」是模板内部标识，**不要改**（改了就对不上模板）
   - 「看板表头」是最终出现在报表里的列名，可改
   - 「取数字段」填真实表物理字段名（见「候选字段清单」），留空表示该维度不取数
   - 「取数方式」：直接取值 / 满意度折算 / 归档判断 / 状态推导
2. 「候选字段清单」—— 真实表 66 列的字段名、中文名、类型、本批填充率，供挑选
3. 「派生规则」—— 各派生方式的参数（关键词、优先级字段等），业务方可调

未配置或文件缺失时，回退到代码内置默认映射，保证平台始终可用。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import config as cfgmod

# 映射表位置：优先放项目根目录（业务方容易找到），其次放 resources
MAPPING_FILENAME = "BI字段对应表.xlsx"
_MAPPING_CANDIDATES = [
    cfgmod.BASE_DIR / MAPPING_FILENAME,
    cfgmod.BASE_DIR / "backend" / "app" / "resources" / MAPPING_FILENAME,
]

# 取数方式
METHOD_DIRECT = "直接取值"
METHOD_SATISFACTION = "满意度折算"
METHOD_ARCHIVED = "归档判断"
METHOD_ARCHIVED_BY_STATUS = "状态判断归档"
METHOD_STATUS = "状态推导"
METHOD_NONE = "不取数"

VALID_METHODS = {
    METHOD_DIRECT, METHOD_SATISFACTION, METHOD_ARCHIVED,
    METHOD_ARCHIVED_BY_STATUS, METHOD_STATUS, METHOD_NONE,
}


# --------------------------------------------------------------------------
# 代码内置默认映射（Excel 缺失时的兜底，也是生成 Excel 的初值）
# --------------------------------------------------------------------------
DEFAULT_MAPPING: list[dict[str, str]] = [
    # 看板维度, 看板表头, 取数字段, 取数方式, 备注
    ("region", "大区", "DQ", METHOD_DIRECT, ""),
    ("province", "省份", "", METHOD_NONE, "真实表无省份字段，不做推断"),
    ("date", "反馈日期", "FKRQ", METHOD_DIRECT, ""),
    ("cat1", "一级分类", "YJFL", METHOD_DIRECT, ""),
    ("cat2", "二级分类", "EJFL", METHOD_DIRECT, ""),
    ("cat3", "三级分类", "SJFL", METHOD_DIRECT, ""),
    ("source", "反馈来源", "FKLY", METHOD_DIRECT, ""),
    ("lab", "智汇实验室分类", "ZHSYSFL", METHOD_DIRECT, ""),
    ("assignee", "指派人员", "LASTNAME3", METHOD_DIRECT, "部门责任人"),
    ("dept", "解决部门", "DEPARTMENTNAME2", METHOD_DIRECT, ""),
    ("responded", "是否响应", "SELECTNAME2", METHOD_DIRECT, ""),
    ("escalated", "是否上升", "SELECTNAME3", METHOD_DIRECT, ""),
    ("escType", "上升类型", "SELECTNAME4", METHOD_DIRECT, ""),
    ("status", "问题状态", "SELECTNAME5", METHOD_STATUS, "缺失时按派生规则补全"),
    ("satisfaction", "客户满意度", "SELECTNAME7", METHOD_SATISFACTION, "按满意度映射表折算成 0–5 分"),
    ("archived", "是否归档", "status", METHOD_ARCHIVED_BY_STATUS,
     "由问题状态判断：状态属于归档状态值即视为已闭环"),
    ("unableReason", "无法解决原因", "GBYYJS", METHOD_DIRECT, ""),
    ("estDate", "预计解决日期", "YJJJRQ", METHOD_DIRECT, ""),
]

# 派生规则默认值：{规则名: 值}
DEFAULT_RULES: dict[str, str] = {
    "归档.日期字段": "WQJJRQ",
    "归档.节点字段": "NODENAME",
    "归档.关键词": "效果评估",
    "归档.状态维度": "status",
    "归档.状态值": "临时解决,完全解决,无法解决",
    "状态.完全解决日期字段": "WQJJRQ",
    "状态.临时解决日期字段": "LSJJRQ",
    "状态.状态字段": "SELECTNAME5",
    "状态.节点字段": "NODENAME",
    "满意度.非常满意": "5",
    "满意度.很满意": "5",
    "满意度.满意": "4",
    "满意度.基本满意": "4",
    "满意度.一般": "3",
    "满意度.非常不满意": "1",
    "满意度.不满意": "2",
    "满意度.默认空值": "",
}

# 维度 → 模板内部标识的顺序（必须与模板一致）
DIMENSION_ORDER = [m[0] for m in DEFAULT_MAPPING]


def mapping_path() -> Path | None:
    """返回实际存在的映射表路径，找不到返回 None。"""
    for p in _MAPPING_CANDIDATES:
        if p.exists():
            return p
    return None


def load_mapping() -> tuple[list[dict[str, str]], dict[str, str], str]:
    """读取映射表。

    返回 (映射行列表, 派生规则, 数据来源说明)。
    读取失败时回退到内置默认，保证不因配置问题导致平台不可用。
    """
    path = mapping_path()
    if path is None:
        return _default_rows(), dict(DEFAULT_RULES), "内置默认映射（未找到 Excel）"

    try:
        import openpyxl
    except ImportError:
        return _default_rows(), dict(DEFAULT_RULES), "内置默认映射（缺少 openpyxl）"

    try:
        wb = openpyxl.load_workbook(path, data_only=True)
    except Exception:
        return _default_rows(), dict(DEFAULT_RULES), f"内置默认映射（{path.name} 读取失败）"

    rows = _read_mapping_sheet(wb)
    rules = _read_rules_sheet(wb)
    if not rows:
        return _default_rows(), dict(DEFAULT_RULES), f"内置默认映射（{path.name} 内容为空）"
    return rows, rules, path.name


def _default_rows() -> list[dict[str, str]]:
    return [
        {
            "dimension": d, "label": lbl, "field": fld, "method": method, "note": note
        }
        for d, lbl, fld, method, note in DEFAULT_MAPPING
    ]


def _read_mapping_sheet(wb) -> list[dict[str, str]]:
    if "字段对应表" not in wb.sheetnames:
        return []
    ws = wb["字段对应表"]
    header = [str(c.value).strip() if c.value is not None else "" for c in ws[1]]
    idx = {name: i for i, name in enumerate(header)}

    need = ("看板维度", "看板表头", "取数字段")
    if not all(k in idx for k in need):
        return []

    out: list[dict[str, str]] = []
    for raw in ws.iter_rows(min_row=2, values_only=True):
        if raw is None:
            continue
        dim = str(raw[idx["看板维度"]]).strip() if raw[idx["看板维度"]] else ""
        if not dim or dim.lower().startswith("看板维度"):
            continue
        label = str(raw[idx["看板表头"]]).strip() if raw[idx["看板表头"]] else dim
        field = str(raw[idx["取数字段"]]).strip() if raw[idx["取数字段"]] else ""
        method = str(raw[idx["取数方式"]]).strip() if "取数方式" in idx and raw[idx["取数方式"]] else ""
        if method not in VALID_METHODS:
            method = METHOD_NONE if not field else METHOD_DIRECT
        note = str(raw[idx["备注"]]).strip() if "备注" in idx and raw[idx["备注"]] else ""
        out.append({"dimension": dim, "label": label, "field": field, "method": method, "note": note})

    # 按模板维度顺序排序，缺失的维度补到末尾
    order = {d: i for i, d in enumerate(DIMENSION_ORDER)}
    out.sort(key=lambda r: order.get(r["dimension"], 999))
    return out


def _read_rules_sheet(wb) -> dict[str, str]:
    rules = dict(DEFAULT_RULES)
    if "派生规则" not in wb.sheetnames:
        return rules
    ws = wb["派生规则"]
    for raw in ws.iter_rows(min_row=2, values_only=True):
        if raw is None or len(raw) < 2:
            continue
        key = str(raw[0]).strip() if raw[0] else ""
        val = str(raw[1]).strip() if raw[1] is not None else ""
        if not key or key.lower().startswith("规则名"):
            continue
        rules[key] = val
    return rules


def build_candidates() -> list[dict[str, Any]]:
    """从最近一次落盘的数据里生成候选字段清单（供 Excel 的「候选字段清单」页）。"""
    from . import bi_dashboard as B

    try:
        df, _, _ = B._load_latest_csv()
    except FileNotFoundError:
        return []

    from .bi_dashboard import _load_columns_meta

    try:
        meta, _ = _load_columns_meta(B.find_latest_run_dir())
    except Exception:
        meta = {}

    total = len(df) or 1
    out: list[dict[str, Any]] = []
    for m in meta:
        name = m.get("fieldName", "")
        if not name or name not in df.columns:
            continue
        series = df[name].astype(str).str.strip()
        filled = int((~series.isin(["", "\\", "-", "None", "null", "NaN"])).sum())
        out.append({
            "fieldName": name,
            "comment": m.get("comment", ""),
            "fieldType": m.get("fieldType", ""),
            "fillRate": round(filled / total, 4),
            "filledRows": filled,
        })
    return out


def write_template_excel(target: Path | None = None) -> Path:
    """生成/覆盖映射表 Excel（含候选字段清单）。"""
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    path = target or _MAPPING_CANDIDATES[0]
    path.parent.mkdir(parents=True, exist_ok=True)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "字段对应表"

    head_fill = PatternFill("solid", fgColor="4472C4")
    head_font = Font(color="FFFFFF", bold=True)

    headers = ["看板维度", "看板表头", "取数字段", "字段中文名", "取数方式", "备注"]
    ws.append(headers)
    for c in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=c)
        cell.fill = head_fill
        cell.font = head_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    candidates = {c["fieldName"]: c for c in build_candidates()}
    for dim, label, field, method, note in DEFAULT_MAPPING:
        cn = candidates.get(field, {}).get("comment", "")
        ws.append([dim, label, field, cn, method, note])

    widths = [14, 16, 18, 20, 12, 40]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"

    # ---- 候选字段清单 ----
    ws2 = wb.create_sheet("候选字段清单")
    h2 = ["字段名", "中文名", "类型", "本批填充率", "非空条数"]
    ws2.append(h2)
    for c in range(1, len(h2) + 1):
        cell = ws2.cell(row=1, column=c)
        cell.fill = head_fill
        cell.font = head_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for item in build_candidates():
        ws2.append([
            item["fieldName"], item["comment"], item["fieldType"],
            item["fillRate"], item["filledRows"],
        ])
    for i, w in enumerate([22, 22, 20, 14, 12], start=1):
        ws2.column_dimensions[get_column_letter(i)].width = w
    ws2.freeze_panes = "A2"
    ws2.auto_filter.ref = f"A1:E{ws2.max_row}"

    # ---- 派生规则 ----
    ws3 = wb.create_sheet("派生规则")
    ws3.append(["规则名", "值", "说明"])
    for c in range(1, 4):
        cell = ws3.cell(row=1, column=c)
        cell.fill = head_fill
        cell.font = head_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
    rule_notes = {
        "归档.日期字段": "该字段有值即视为已闭环",
        "归档.节点字段": "流程节点字段名",
        "归档.关键词": "节点包含该关键词即视为已闭环",
        "状态.完全解决日期字段": "有值 → 状态判为「完全解决」",
        "状态.临时解决日期字段": "有值 → 状态判为「临时解决」",
        "状态.状态字段": "原始问题状态字段名",
        "状态.节点字段": "原始状态为空时按流程节点兜底",
        "满意度.非常满意": "折算分数",
        "满意度.很满意": "折算分数",
        "满意度.满意": "折算分数",
        "满意度.基本满意": "折算分数",
        "满意度.一般": "折算分数",
        "满意度.非常不满意": "折算分数",
        "满意度.不满意": "折算分数",
        "满意度.默认空值": "未匹配到档位时的取值，留空表示不计入均值",
    }
    for k, v in DEFAULT_RULES.items():
        ws3.append([k, v, rule_notes.get(k, "")])
    for i, w in enumerate([26, 22, 44], start=1):
        ws3.column_dimensions[get_column_letter(i)].width = w
    ws3.freeze_panes = "A2"

    wb.save(path)
    return path


def dump_state() -> str:
    """给接口用的映射摘要（便于前端展示当前生效的配置来源）。"""
    rows, rules, source = load_mapping()
    return json.dumps(
        {
            "source": source,
            "path": str(mapping_path()) if mapping_path() else "",
            "rows": rows,
            "rules": rules,
        },
        ensure_ascii=False,
    )
