"""服务质量需求 BI 看板的数据层。

把中台真实表 `ADS_SERVICE_DEMAND_FEEDBACK`（服务需求反馈，66 列流程表）
转换成《服务质量需求BI看板（模板）.html》能直接消费的中文表头结构。

设计原则
--------
1. **不编造字段**。每个模板维度都必须在真实表里有对应物理字段；缺失的维度
   显式标注来源与派生依据，写在 DIMENSION_SOURCES 里，供口径说明引用。
2. **全量数据**。看板要能下载、能复核，因此这里读的是落盘的完整 CSV，
   不是解析阶段的 200 行预览。
3. **中文表头输出**。模板内部用中文表头做模糊匹配（buildColMap），
   所以对外统一输出中文键，模板无需改动即可消费。

物理字段 → 模板维度
--------------------
region        大区                ← DQ「大区」                        直接映射
date          反馈日期            ← FKRQ「反馈日期」                   直接映射
cat1          一级分类            ← YJFL「一级分类」                   直接映射
cat2          二级分类            ← EJFL「二级分类」                   直接映射
cat3          三级分类            ← SJFL「三级分类」                   直接映射
source        反馈来源            ← FKLY「反馈来源」                   直接映射
lab           智汇实验室分类      ← ZHSYSFL「智汇实验室分类」          直接映射
dept          解决部门            ← DEPARTMENTNAME2「解决部门」       直接映射
responded     是否响应            ← SELECTNAME2「是否响应」            直接映射
escalated     是否上升            ← SELECTNAME3「是否上升」            直接映射
escType       上升类型            ← SELECTNAME4「上升类型」            直接映射
status        问题状态            ← SELECTNAME5「问题状态」            直接映射
estDate       预计解决日期        ← YJJJRQ「预计解决日期」             直接映射
assignee      指派人员            ← LASTNAME3「部门责任人」            语义最接近
unableReason  无法解决原因        ← GBYYJS「根本原因简述」            原因类字段
archived      是否归档            ← NODENAME「流程节点」派生              见下
satisfaction  客户满意度          ← SELECTNAME7「客户满意度」          需转 0–5 整数

派生口径（DIFF_AREAS 内记录，供看板口径说明引用）
----------------------------------------------
- **是否归档** 由「流程节点」(NODENAME) 派生：节点包含「效果评估」即视为已闭环归档。
  注意不能用 XGPG「效果评估」列——该列在真实数据中整列为空。
- **客户满意度** 真实值为中文档位，需映射成 0–5 整数才能参与均值计算；
  映射表见 SATISFACTION_MAP，空档位视为未回收（null）。
- **省份** 真实表不含省份字段（仅有大区），看板中该维度留空不做推断。
"""
from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import Any

import pandas as pd

from . import config as cfgmod

FIXED_TABLE = "ADS_SERVICE_DEMAND_FEEDBACK"

# 流程节点字段：用于派生「是否归档」（真实表没有名为「归档」的字段）
NODE_FIELD = "NODENAME"
ARCHIVE_NODE = "效果评估"

# --------------------------------------------------------------------------
# 映射配置：优先读业务方维护的「BI字段对应表.xlsx」，读不到用内置默认
# --------------------------------------------------------------------------
from . import bi_mapping  # noqa: E402  (循环依赖：bi_mapping 需要本模块取候选字段)

# 空值占位符（真实表用 \ 或 - 表示无内容）
PLACEHOLDERS = {"", "\\", "-", "None", "null", "NULL", "NaN"}


def current_mapping() -> tuple[list[dict[str, str]], dict[str, str], str]:
    """返回 (映射行, 派生规则, 配置来源)。"""
    return bi_mapping.load_mapping()


def _rules_value(rules: dict[str, str], key: str, default: str = "") -> str:
    v = rules.get(key)
    return v if v is not None else default


def _build_satisfaction_map(rules: dict[str, str]) -> dict[str, int]:
    """由派生规则构造满意度档位 → 分数映射。"""
    out: dict[str, int] = {}
    for key, val in rules.items():
        if not key.startswith("满意度."):
            continue
        label = key.split(".", 1)[1]
        if not label or not val:
            continue
        try:
            out[label] = int(val)
        except ValueError:
            continue
    # 数字档位兜底
    for i in range(0, 6):
        out.setdefault(str(i), i)
    return out


def _to_satisfaction(raw: str, sat_map: dict[str, int]) -> int | None:
    if not raw:
        return None
    if raw in sat_map:
        return sat_map[raw]
    m = re.search(r"(\d)", raw)
    if m:
        v = int(m.group(1))
        return v if 0 <= v <= 5 else None
    return None


def _status_from_node(node: str, rules: dict[str, str]) -> str:
    """流程节点 → 模板口径的问题状态（原状态字段为空时的兜底）。"""
    if not node:
        return ""
    if "效果评估" in node:
        return "完全解决"
    if "过程监控" in node:
        return "待解决"
    if "实施" in node:
        return "临时解决"
    return ""


def _derive_archived(raw: Any, df: pd.DataFrame, rules: dict[str, str]) -> str:
    """归档判断：日期字段有值，或节点字段包含关键词。"""
    date_field = _rules_value(rules, "归档.日期字段")
    node_field = _rules_value(rules, "归档.节点字段")
    keyword = _rules_value(rules, "归档.关键词", "效果评估")
    if date_field and date_field in df.columns:
        if _clean(raw.get(date_field)):
            return "是"
    if node_field and node_field in df.columns:
        if keyword and keyword in _clean(raw.get(node_field)):
            return "是"
    return "否"


def _derive_status(raw: Any, df: pd.DataFrame, rules: dict[str, str]) -> str:
    """问题状态：完全解决日期 > 临时解决日期 > 原始状态字段 > 流程节点兜底。"""
    full_f = _rules_value(rules, "状态.完全解决日期字段")
    temp_f = _rules_value(rules, "状态.临时解决日期字段")
    stat_f = _rules_value(rules, "状态.状态字段")
    node_f = _rules_value(rules, "状态.节点字段")

    if full_f and full_f in df.columns and _clean(raw.get(full_f)):
        return "完全解决"
    if temp_f and temp_f in df.columns and _clean(raw.get(temp_f)):
        return "临时解决"
    if stat_f and stat_f in df.columns:
        v = _clean(raw.get(stat_f))
        if v:
            return v
    if node_f and node_f in df.columns:
        return _status_from_node(_clean(raw.get(node_f)), rules)
    return ""


def _archive_status_values(rules: dict[str, str]) -> set[str]:
    raw = _rules_value(rules, "归档.状态值")
    return {v.strip() for v in re.split(r"[,，、|;；]", raw) if v.strip()}


def _derive_archived_by_status(status_value: str, rules: dict[str, str]) -> str:
    """按「问题状态」判断是否归档。

    业务口径：问题状态 ∈ 派生规则「归档.状态值」（默认 临时解决/完全解决/无法解决）
    时视为已闭环归档；状态为空则视为未归档。
    """
    targets = _archive_status_values(rules)
    if not targets:
        return ""
    return "是" if _clean(status_value) in targets else "否"


def _clean(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    if s in PLACEHOLDERS:
        return ""
    return s


def find_latest_run_dir() -> Path | None:
    """找出最近一次落盘且包含目标数据表的 run 目录。

    必须按目标表过滤：data/ 下历史测试拉取会留下 run_xxx/tables/A.csv 之类的
    垃圾目录，直接取时间最新的目录会拿到测试数据而不是真实业务数据。
    """
    root = cfgmod.DATA_DIR
    if not root.exists():
        return None
    runs = sorted(
        (p for p in root.glob("run_*") if p.is_dir()),
        key=lambda p: p.name,
        reverse=True,
    )
    for run in runs:
        if (run / "tables" / f"{FIXED_TABLE}.csv").exists():
            return run
    return None


def _load_latest_csv() -> tuple[pd.DataFrame, str, str]:
    """读最近一次落盘的表 CSV，返回 (df, 表名, 表中文名)。"""
    run_dir = find_latest_run_dir()
    if not run_dir:
        raise FileNotFoundError("本地还没有落盘的数据，请先执行一次拉取")
    tables = list((run_dir / "tables").glob("*.csv"))
    if not tables:
        raise FileNotFoundError(f"{run_dir} 下没有表数据文件")
    target = run_dir / "tables" / f"{FIXED_TABLE}.csv"
    csv_path = target if target.exists() else tables[0]
    df = pd.read_csv(csv_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    df = df.fillna("")

    chinese_name = ""
    cols_file = csv_path.with_suffix(".columns.json")
    if cols_file.exists():
        try:
            meta = json.loads(cols_file.read_text(encoding="utf-8"))
            chinese_name = str(meta.get("chineseName") or "")
        except Exception:
            chinese_name = ""
    return df, FIXED_TABLE, chinese_name


def _load_columns_meta(run_dir: Path | None) -> tuple[list[dict[str, Any]], str]:
    """读 columns.json，返回 (列元数据列表, 表中文名)。"""
    if not run_dir:
        return [], ""
    cols_file = run_dir / "tables" / f"{FIXED_TABLE}.columns.json"
    if not cols_file.exists():
        return [], ""
    try:
        meta = json.loads(cols_file.read_text(encoding="utf-8"))
    except Exception:
        return [], ""
    cols = meta.get("columns") or []
    if isinstance(cols, dict):
        cols = [{"fieldName": k, **(v if isinstance(v, dict) else {"comment": v})}
                for k, v in cols.items()]
    return cols, str(meta.get("chineseName") or "")


def build_dataset(limit: int = 20000) -> dict[str, Any]:
    """构造看板数据集（按「BI字段对应表.xlsx」的配置填数）。

    返回：
        {
          "table": "ADS_SERVICE_DEMAND_FEEDBACK",
          "chineseName": "服务需求反馈",
          "rowCount": 5,
          "columns": [{"key": "大区", "dimension": "region", "label": "大区"}, ...],
          "rows": [{"大区": "华东一区", ...}, ...],
          "dimensions": {dim: label},
          "diffAreas": [...],
          "mappingSource": "BI字段对应表.xlsx",
        }
    """
    df, table_name, chinese_name = _load_latest_csv()
    mapping, rules, source = current_mapping()
    sat_map = _build_satisfaction_map(rules)

    columns: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    dim_labels: dict[str, str] = {
        m["dimension"]: (m["label"] or m["dimension"]) for m in mapping
    }

    for m in mapping:
        dim = m["dimension"]
        label = m["label"] or dim
        field = m.get("field") or ""
        method = m.get("method") or bi_mapping.METHOD_NONE
        has_field = bool(field) and field in df.columns
        if method == bi_mapping.METHOD_ARCHIVED_BY_STATUS:
            has_field = True  # 由问题状态派生，不依赖物理字段
        columns.append({
            "key": label, "dimension": dim, "label": label,
            "available": has_field and method != bi_mapping.METHOD_NONE,
            "field": field, "method": method, "note": m.get("note", ""),
        })

    for _, raw in df.head(limit).iterrows():
        row: dict[str, Any] = {}
        # 第一遍：填所有非「状态判断归档」的维度
        for m in mapping:
            dim = m["dimension"]
            label = m["label"] or dim
            field = m.get("field") or ""
            method = m.get("method") or bi_mapping.METHOD_NONE

            if method == bi_mapping.METHOD_ARCHIVED_BY_STATUS:
                row[label] = ""  # 第二遍再填，依赖已算出的问题状态
                continue
            if method == bi_mapping.METHOD_NONE or not field or field not in df.columns:
                row[label] = ""
                continue
            if method == bi_mapping.METHOD_ARCHIVED:
                row[label] = _derive_archived(raw, df, rules)
                continue
            if method == bi_mapping.METHOD_STATUS:
                row[label] = _derive_status(raw, df, rules)
                continue

            val = _clean(raw.get(field))
            if method == bi_mapping.METHOD_SATISFACTION:
                score = _to_satisfaction(val, sat_map)
                row[label] = score if score is not None else ""
            else:
                row[label] = val

        # 第二遍：归档依赖「问题状态」，必须在状态算完之后
        for m in mapping:
            if (m.get("method") or bi_mapping.METHOD_NONE) != bi_mapping.METHOD_ARCHIVED_BY_STATUS:
                continue
            label = m["label"] or m["dimension"]
            status_dim = (_rules_value(rules, "归档.状态维度") or m.get("field") or "status").strip()
            status_label = dim_labels.get(status_dim, status_dim)
            row[label] = _derive_archived_by_status(row.get(status_label, ""), rules)

        rows.append(row)

    diff_areas = _build_diff_areas(mapping, rules)

    return {
        "table": table_name,
        "chineseName": chinese_name or "服务需求反馈",
        "rowCount": int(len(df)),
        "returnedRows": len(rows),
        "columns": columns,
        "rows": rows,
        "dimensions": {m["dimension"]: (m["label"] or m["dimension"]) for m in mapping},
        "diffAreas": diff_areas,
        "mappingSource": source,
    }


def _build_diff_areas(mapping: list[dict[str, str]], rules: dict[str, str]) -> list[str]:
    """按当前映射生成口径说明（业务方改表后说明自动跟着变）。"""
    areas: list[str] = []
    for m in mapping:
        method = m.get("method")
        label = m["label"] or m["dimension"]
        field = m.get("field") or ""
        note = (m.get("note") or "").strip()

        if method == bi_mapping.METHOD_NONE:
            if note:
                areas.append(f"「{label}」{note}。")
            continue
        if method == bi_mapping.METHOD_ARCHIVED_BY_STATUS:
            status_dim = _rules_value(rules, "归档.状态维度") or "status"
            status_label = status_dim
            for mm in mapping:
                if mm["dimension"] == status_dim:
                    status_label = mm["label"] or status_dim
                    break
            vals = "、".join(sorted(_archive_status_values(rules)))
            areas.append(f"「{label}」由「{status_label}」判断：状态为 {vals} 时视为已归档。")
            continue
        if method == bi_mapping.METHOD_ARCHIVED:
            areas.append(
                f"「{label}」为派生字段：{_rules_value(rules, '归档.日期字段')} 有值，"
                f"或 {_rules_value(rules, '归档.节点字段')} 含「{_rules_value(rules, '归档.关键词')}」时判为已闭环。"
            )
            continue
        if method == bi_mapping.METHOD_STATUS:
            areas.append(
                f"「{label}」优先取 {_rules_value(rules, '状态.状态字段')}，"
                f"缺失时按完全解决日期 / 临时解决日期 / 流程节点补全。"
            )
            continue
        if method == bi_mapping.METHOD_SATISFACTION:
            areas.append(f"「{label}」原值为中文档位，已按派生规则折算为 0–5 分。")
            continue
        if note:
            areas.append(f"「{label}」取真实表「{field}」{note}。")
    return areas


def dataset_to_xlsx(dataset: dict[str, Any]) -> bytes:
    """把数据集导出为 xlsx（业务人员可直接下载核对）。"""
    buf = io.BytesIO()
    labels = [c["label"] for c in dataset["columns"] if c.get("available", True)]
    frame = pd.DataFrame(
        [[r.get(label, "") for label in labels] for r in dataset["rows"]],
        columns=labels,
    )
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        frame.to_excel(writer, sheet_name="服务需求反馈", index=False)
        # 口径说明页
        notes = pd.DataFrame({
            "项目": ["数据表", "表中文名", "总行数", "导出行数"] +
                    [f"维度 {c['dimension']}" for c in dataset["columns"]] +
                    ["派生口径"] + [f"口径{i+1}" for i in range(len(dataset["diffAreas"]))],
            "说明": [
                dataset["table"], dataset["chineseName"],
                str(dataset["rowCount"]), str(dataset["returnedRows"]),
            ] + [
                f"{c['label']}（{c['dimension']}）" + ("" if c.get("available", True) else " · 真实表无此字段")
                for c in dataset["columns"]
            ] + [""] + dataset["diffAreas"],
        })
        notes.to_excel(writer, sheet_name="口径说明", index=False)
    return buf.getvalue()
