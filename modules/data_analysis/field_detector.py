from __future__ import annotations

FIELD_HINTS: dict[str, tuple[str, ...]] = {
    "time": ("检测完成时间", "完成时间", "出结果时间", "完成时刻", "检测时间", "上机时间", "到单时间", "时间", "日期"),
    "module": ("模块", "仪器", "设备", "机台"),
    "project": ("项目名称", "检测项目", "项目", "检验项目"),
    "hospital": ("医院名称", "医疗机构", "医院", "实验室", "院区"),
    "department": ("处理部门", "送检科室", "申请科室", "科室", "部门"),
    "owner": ("负责人", "跟进人", "对接人", "处理人"),
    "satisfaction": ("满意度评分", "满意度", "评价分数"),
}

FIELD_LABELS = {
    "time": "时间列",
    "module": "模块列",
    "project": "项目名称列",
    "hospital": "医院/实验室列",
    "department": "部门列",
    "owner": "负责人列",
    "satisfaction": "满意度列",
}


def detect_fields(columns: list[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    used: set[str] = set()

    for role, hints in FIELD_HINTS.items():
        for hint in hints:
            exact = next((column for column in columns if column not in used and column == hint), None)
            partial = next((column for column in columns if column not in used and hint in column), None)
            column = exact or partial
            if column:
                found[role] = column
                used.add(column)
                break

    return found


def detect_business_fields(columns: list[str]) -> dict[str, str]:
    found = detect_fields(columns)
    return {role: column for role, column in found.items() if role in {"hospital", "department", "owner", "satisfaction", "time"}}


def required_field_warnings(columns: list[str]) -> list[str]:
    found = detect_fields(columns)
    if {"二级分类", "三级分类", "需求内容"} & set(columns):
        return [FIELD_LABELS[role] for role in ("hospital", "department", "owner", "satisfaction") if role not in found]
    return [FIELD_LABELS[role] for role in ("time", "module", "project") if role not in found]
