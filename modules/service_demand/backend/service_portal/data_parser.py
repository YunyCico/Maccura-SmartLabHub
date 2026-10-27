"""按《对外数据拉取接口（文件下载版）》第 8.2 节解析 ZIP。

文档约定的 ZIP 结构：
    tables/<name>.columns.json   表字段元数据（fieldName/comment/fieldType/length）
    tables/<name>.csv            表数据 CSV，第一行为字段名
    metrics/<code>.json          指标结果
    combinations/<code>.json     指标组合结果
    manifest.json                清单

CSV 编码不固定（平台侧导出常见 UTF-8 BOM 或 GBK），逐个回退。
"""
from __future__ import annotations

import io
import json
import math
import zipfile
from typing import Any

import pandas as pd

ENCODINGS = ["utf-8-sig", "utf-8", "gb18030", "gbk", "latin1"]
MAX_PREVIEW_ROWS = 200
MAX_UNIQUE_FOR_TOP = 50
# 业务分布图用的完整计数上限：基数不超过该值的列，返回全量取值分布
MAX_UNIQUE_FOR_DIST = 100


class PlatformParseError(Exception):
    pass


def _read_csv(raw: bytes) -> pd.DataFrame:
    last_err: Exception | None = None
    for enc in ENCODINGS:
        try:
            return pd.read_csv(
                io.BytesIO(raw), encoding=enc, dtype=str,
                keep_default_na=False, on_bad_lines="skip",
            )
        except UnicodeDecodeError as e:
            last_err = e
        except pd.errors.EmptyDataError:
            return pd.DataFrame()
        except Exception as e:
            last_err = e
    raise PlatformParseError(f"CSV 编码无法识别：{last_err}")


def _read_json(raw: bytes) -> Any:
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return json.loads(raw.decode(enc))
        except UnicodeDecodeError:
            continue
        except Exception:
            return None
    return None


def _jsonable(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, float):
        return None if (math.isnan(v) or math.isinf(v)) else v
    if isinstance(v, (int, str, bool)):
        return v
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


def profile_column(series: pd.Series) -> dict[str, Any]:
    total = int(len(series))
    filled = [s for s in series.astype(str).tolist() if s.strip() != ""]
    uniq = sorted(set(filled))

    numeric = 0
    for s in filled:
        try:
            float(s)
            numeric += 1
        except (ValueError, TypeError):
            break

    kind = "文本"
    if filled:
        if numeric == len(filled):
            kind = "数值"
        elif len(uniq) <= max(2, len(filled) // 10):
            kind = "枚举"

    top: list[dict[str, Any]] = []
    if filled and len(uniq) <= MAX_UNIQUE_FOR_TOP:
        counts = pd.Series(filled).value_counts().head(8)
        top = [{"value": str(i), "count": int(c)} for i, c in counts.items()]

    # 业务分布：基数不高时给出全量取值计数（按出现次数降序）
    dist: list[dict[str, Any]] = []
    if filled and len(uniq) <= MAX_UNIQUE_FOR_DIST:
        counts = pd.Series(filled).value_counts()
        dist = [{"value": str(i), "count": int(c)} for i, c in counts.items()]

    # 数值列统计：合计 / 均值 / 最小 / 最大（业务指标用）
    num_stats: dict[str, Any] = {}
    if filled and numeric == len(filled):
        nums = pd.Series([float(s) for s in filled])
        num_stats = {
            "sum": round(float(nums.sum()), 4),
            "avg": round(float(nums.mean()), 4),
            "min": float(nums.min()),
            "max": float(nums.max()),
        }

    # 日期列（YYYY-MM-DD 等 ISO 形式）取最早 / 最晚，用于业务时间范围
    date_range: dict[str, Any] = {}
    if filled:
        iso = [s for s in filled if len(s) >= 10 and s[4] == "-" and s[7] == "-"]
        if len(iso) == len(filled):
            date_range = {"min": min(iso), "max": max(iso)}

    return {
        "nonEmpty": len(filled),
        "nonEmptyRate": round(len(filled) / total, 4) if total else 0,
        "uniqueCount": len(uniq),
        "kind": kind,
        "top": top,
        "dist": dist,
        "numStats": num_stats,
        "dateRange": date_range,
        "samples": filled[:5],
    }


def analyze_dataframe(
    df: pd.DataFrame, name: str, columns_meta: dict[str, dict[str, str]] | None = None
) -> dict[str, Any]:
    """columns_meta: 物理字段名 -> {fieldName, comment, fieldType, length}"""
    cols = [str(c) for c in df.columns]
    preview_rows = [
        {c: _jsonable(row[c]) for c in df.columns}
        for _, row in df.head(MAX_PREVIEW_ROWS).iterrows()
    ]
    fields: list[dict[str, Any]] = []
    for c in df.columns:
        meta = (columns_meta or {}).get(str(c), {})
        fields.append({
            "name": str(c),
            "comment": meta.get("comment") or "",
            "fieldType": meta.get("fieldType") or "",
            "length": meta.get("length") or "",
            **profile_column(df[c]),
        })
    return {
        "name": name,
        "rowCount": int(len(df)),
        "columnCount": int(len(cols)),
        "columns": cols,
        "fields": fields,
        "preview": preview_rows,
    }


def _load_columns_meta(zf: zipfile.ZipFile) -> tuple[dict[str, dict[str, dict[str, str]]], dict[str, str]]:
    """读所有 *.columns.json，返回 ({表名: {字段名: 元数据}}, {表名: 中文名})。"""
    out: dict[str, dict[str, dict[str, str]]] = {}
    names: dict[str, str] = {}
    for name in zf.namelist():
        if not name.endswith(".columns.json"):
            continue
        data = _read_json(zf.read(name))
        if not isinstance(data, dict):
            continue
        table = str(data.get("tableName") or name.split("/")[-1].replace(".columns.json", ""))
        if data.get("chineseName"):
            names[table] = str(data["chineseName"])
        cols: dict[str, dict[str, str]] = {}
        for c in data.get("columns") or []:
            if isinstance(c, dict) and c.get("fieldName"):
                cols[str(c["fieldName"])] = {
                    "comment": str(c.get("comment") or ""),
                    "fieldType": str(c.get("fieldType") or ""),
                    "length": str(c.get("length") or ""),
                }
        out[table] = cols
    return out, names


def parse_zip(zip_bytes: bytes) -> dict[str, Any]:
    """解析整个 ZIP。"""
    result: dict[str, Any] = {
        "manifest": {}, "entries": [],
        "datasets": [], "metrics": [], "combinations": [], "errors": [],
    }
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as e:
        raise PlatformParseError(f"返回内容不是有效的 ZIP：{e}")

    with zf:
        result["entries"] = [
            {"name": i.filename, "size": i.file_size, "kind": _kind(i.filename)}
            for i in zf.infolist() if not i.is_dir()
        ]

        if "manifest.json" in zf.namelist():
            m = _read_json(zf.read("manifest.json"))
            if isinstance(m, dict):
                result["manifest"] = m

        cols_meta, table_cn = _load_columns_meta(zf)

        for name in zf.namelist():
            if name.endswith("/"):
                continue
            try:
                if name == "manifest.json" or name.endswith(".columns.json"):
                    continue

                if name.startswith("tables/") and name.endswith(".csv"):
                    df = _read_csv(zf.read(name))
                    tname = name.split("/")[-1].replace(".csv", "")
                    ds = analyze_dataframe(df, tname, cols_meta.get(tname))
                    ds["kind"] = "table"
                    ds["path"] = name
                    ds["chineseName"] = table_cn.get(tname, "")
                    result["datasets"].append(ds)

                elif name.startswith("metrics/") and name.endswith(".json"):
                    data = _read_json(zf.read(name))
                    result["metrics"].append({
                        "code": name.split("/")[-1].replace(".json", ""),
                        "path": name,
                        "payload": data,
                    })

                elif name.startswith("combinations/") and name.endswith(".json"):
                    data = _read_json(zf.read(name))
                    result["combinations"].append({
                        "code": name.split("/")[-1].replace(".json", ""),
                        "path": name,
                        "payload": data,
                    })

                elif name.endswith(".csv"):
                    # 兼容没有 tables/ 前缀的扁平结构
                    df = _read_csv(zf.read(name))
                    tname = name.split("/")[-1].replace(".csv", "")
                    ds = analyze_dataframe(df, tname, cols_meta.get(tname))
                    ds["kind"] = "table"
                    ds["path"] = name
                    ds["chineseName"] = table_cn.get(tname, "")
                    result["datasets"].append(ds)

            except Exception as e:
                result["errors"].append(f"{name}：{e}")

    return result


def _kind(name: str) -> str:
    if name == "manifest.json":
        return "manifest"
    if name.endswith(".columns.json"):
        return "columns"
    if name.startswith("tables/") or name.endswith(".csv"):
        return "table"
    if name.startswith("metrics/"):
        return "metric"
    if name.startswith("combinations/"):
        return "combination"
    return "other"


def flatten_metric_payload(payload: Any) -> dict[str, Any]:
    """把指标/组合 JSON 压成"行列表"，尽量转成可表格展示的结构。"""
    if payload is None:
        return {"columns": [], "rows": []}
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        for key in ("data", "rows", "list", "result", "records"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
        else:
            rows = [payload]
    else:
        rows = [{"value": payload}]

    cols: list[str] = []
    for r in rows[:50]:
        if isinstance(r, dict):
            for k in r:
                if k not in cols:
                    cols.append(str(k))
    out_rows = []
    for r in rows:
        if isinstance(r, dict):
            out_rows.append({c: _jsonable(r.get(c)) for c in cols})
        else:
            out_rows.append({"value": _jsonable(r)})
            if "value" not in cols:
                cols.append("value")
    return {"columns": cols, "rows": out_rows}