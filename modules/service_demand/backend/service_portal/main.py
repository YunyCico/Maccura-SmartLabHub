"""FastAPI 主服务：参数校验 → 调开放接口 → 解析 ZIP → 返回结构化结果。

按《对外数据拉取接口（文件下载版）》实现校验规则：
- pageNo 与 pageSize 必须同时传入，pageNo ≥ 1，1 ≤ pageSize ≤ 10000，否则 400
- condition 的 operator 必须是文档枚举内的 7 个之一
- condition 的 key 必须是物理表名，field 必须是物理字段名
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

from . import bi_dashboard
from . import bi_mapping
from . import config as cfgmod
from .data_parser import flatten_metric_payload, parse_zip
from .platform_client import (
    TOKEN_CACHE,
    ZIP_TTL_SECONDS,
    DataPlatformClient,
    PlatformError,
    extract_zip,
    list_zip_entries,
    now_stamp,
)

app = FastAPI(title="数据中台 · 对外数据拉取控制台", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

HISTORY_FILE = cfgmod.DATA_DIR / "history.json"
FIELD_CACHE_FILE = cfgmod.DATA_DIR / "_field_cache.json"
MAX_HISTORY = 50
# 筛选下拉最多给多少个候选值
MAX_FIELD_VALUES = 60

# 文档 6.3 operator 枚举
OPERATORS = ("eq", "like", "gte", "lte", "in", "isNull", "isNotNull")
OPERATOR_LABELS = {
    "eq": "等于", "like": "包含/模糊", "gte": "大于等于", "lte": "小于等于",
    "in": "在范围内", "isNull": "为空", "isNotNull": "不为空",
}
# 文档 10.10：字段名只允许字母、数字、下划线，且以字母或下划线开头
FIELD_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
NO_VALUE_OPS = {"isNull", "isNotNull"}

# 页面筛选字段的业务化元数据。默认表来自官方示例 ZIP；未知表不允许退回任意文本输入，
# 避免用户提交不存在的物理字段。后续可由平台元数据接口替换此映射。
TABLE_FIELDS = {
    "ADS_SERVICE_DEMAND_FEEDBACK": [
        {"name": "REQUESTID", "comment": "需求单ID", "fieldType": "BIGINT", "values": [str(n) for n in range(5298479, 5298493)]},
        {"name": "DABH", "comment": "单据编号", "fieldType": "VARCHAR(32)", "values": [f"FWXQSQ20261009{i:04d}" for i in range(1, 15)]},
        {"name": "YJFL", "comment": "一级分类", "fieldType": "VARCHAR(64)", "values": ["服务质量需求", "服务态度需求"]},
        {"name": "EJFL", "comment": "二级分类", "fieldType": "VARCHAR(64)", "values": ["授课", "培训", "驻场", "答疑"]},
        {"name": "ZDKH", "comment": "重点客户", "fieldType": "VARCHAR(128)", "values": ["柳林县人民医院", "成都市第三人民医院", "绵阳市中心医院", "南充市中心医院"]},
        {"name": "DQ", "comment": "地区", "fieldType": "VARCHAR(64)", "values": ["华东一区", "西南一区", "西南二区"]},
        {"name": "LASTNAME1", "comment": "负责人", "fieldType": "VARCHAR(32)", "values": ["孟鹏杰", "张伟", "李娜", "王强"]},
        {"name": "DEPARTMENTNAME1", "comment": "处理部门", "fieldType": "VARCHAR(128)", "values": ["实验室运营管理部"]},
        {"name": "CREATEDATE", "comment": "提交日期", "fieldType": "DATE", "values": [f"2026-10-{d:02d}" for d in range(1, 15)]},
        {"name": "SCORE", "comment": "满意度评分", "fieldType": "DECIMAL(4,2)", "values": ["3", "3.5", "3.8", "4", "4.2", "4.5", "4.6", "4.7", "5"]},
        {"name": "ISUPGRADE", "comment": "是否升级", "fieldType": "VARCHAR(8)", "values": ["是", "否"]},
        {"name": "REPLYCNT", "comment": "响应次数", "fieldType": "INT", "values": [str(n) for n in range(1, 5)]},
    ],
    "ADS_SATISFACTION_DIM": [
        {"name": "HOSPITAL", "comment": "医院名称", "fieldType": "VARCHAR(128)", "values": []},
        {"name": "DIMNAME", "comment": "评价维度", "fieldType": "VARCHAR(64)", "values": []},
        {"name": "SCORE", "comment": "得分", "fieldType": "DECIMAL(4,2)", "values": []},
        {"name": "JOINCNT", "comment": "评价人数", "fieldType": "INT", "values": []},
        {"name": "PROBLEMCNT", "comment": "问题数", "fieldType": "INT", "values": []},
    ],
}


# ---------------- 请求模型 ----------------

class ConditionItem(BaseModel):
    field: str
    operator: Literal["eq", "like", "gte", "lte", "in", "isNull", "isNotNull"]
    value: Any = ""

    @field_validator("field")
    @classmethod
    def check_field(cls, v: str) -> str:
        v = (v or "").strip()
        if not FIELD_NAME_RE.match(v):
            raise ValueError(
                f"field「{v}」必须是物理字段名，只能含字母/数字/下划线且以字母或下划线开头"
            )
        return v

    @model_validator(mode="after")
    def check_value(self):
        if self.operator == "in":
            v = self.value
            ok = (isinstance(v, list) and len(v) > 0) or (
                isinstance(v, str) and len(v.strip()) > 0
            )
            if not ok:
                raise ValueError("operator=in 需要数组或英文逗号分隔字符串")
        elif self.operator not in NO_VALUE_OPS:
            if self.value is None or (isinstance(self.value, str) and self.value == ""):
                raise ValueError(f"operator={self.operator} 需要提供 value")
        return self


class PullRequest(BaseModel):
    username: str | None = None
    busDomain: list[str] | None = None
    tableName: list[str] | None = None
    metricsDomain: list[str] | None = None
    metricsName: list[str] | None = None
    combinationDomain: list[str] | None = None
    combinationName: list[str] | None = None
    condition: dict[str, list[ConditionItem]] | None = None
    pageNo: int | None = Field(default=None, ge=1)
    pageSize: int | None = Field(default=None, ge=1, le=10000)

    @model_validator(mode="after")
    def check_paging(self):
        # 文档 5.3：只传一个 → 400
        if (self.pageNo is None) != (self.pageSize is None):
            raise ValueError("pageNo 与 pageSize 必须同时传入（只传一个会返回 400）")
        return self


class PassRateRequest(BaseModel):
    username: str | None = None
    vdatumStart: str | None = None
    vdatumStop: str | None = None
    werk: str | None = Field(default=None, max_length=4)
    lifnr: str = Field(..., max_length=50, description="供应商编码，必填")


class DeliveryRateRequest(BaseModel):
    username: str | None = None
    supplierCode: str | None = Field(default=None, max_length=20)
    startDate: str | None = None
    stopDate: str | None = None
    factory: str | None = Field(default=None, max_length=10)


class ConfigPatch(BaseModel):
    username: str | None = None
    openApiKey: str | None = None


# ---------------- 工具 ----------------

def _clean(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in payload.items():
        if v is None:
            continue
        if isinstance(v, (list, str, dict)) and len(v) == 0:
            continue
        out[k] = v
    return out


def _load_history() -> list[dict[str, Any]]:
    if not HISTORY_FILE.exists():
        return []
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_history(item: dict[str, Any]) -> None:
    cfgmod._ensure_dir()
    hist = _load_history()
    hist.insert(0, item)
    HISTORY_FILE.write_text(
        json.dumps(hist[:MAX_HISTORY], ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _load_field_cache() -> dict[str, Any]:
    if not FIELD_CACHE_FILE.exists():
        return {}
    try:
        return json.loads(FIELD_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _update_field_cache(datasets: list[dict[str, Any]]) -> None:
    """把最近一次真实拉取到的字段与取值写入本地缓存。

    页面筛选下拉的字段列表与候选值全部来自这里，保证与中台真实表结构一致，
    不再依赖硬编码的示例字段。
    """
    cache = _load_field_cache()
    for ds in datasets:
        table = str(ds.get("name") or "")
        # 过滤明显是测试用的表名（如 "A"），避免污染筛选下拉
        if not table or len(table) < 4 or not table[0].isalpha() or " " in table:
            continue
        fields = []
        for f in ds.get("fields") or []:
            values = [
                str(x["value"])
                for x in (f.get("dist") or [])[:MAX_FIELD_VALUES]
                if str(x.get("value", "")).strip() != ""
            ]
            fields.append({
                "name": f.get("name"),
                "comment": f.get("comment") or "",
                "fieldType": f.get("fieldType") or "",
                "values": values,
            })
        cache[table] = {
            "chineseName": ds.get("chineseName") or "",
            "updatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "fields": fields,
        }
    try:
        cfgmod._ensure_dir()
        FIELD_CACHE_FILE.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def _effective_table_fields() -> dict[str, Any]:
    """真实缓存优先，缺失时回退到内置示例字段。"""
    merged: dict[str, Any] = {}
    for table, meta in _load_field_cache().items():
        fields = meta.get("fields") or []
        if fields:
            merged[table] = fields
    for table, fields in TABLE_FIELDS.items():
        if table not in merged:
            merged[table] = fields
    return merged


def _fail(message: str, code: int | None = None, status: int = 400) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"ok": False, "message": message, "code": code}
    )


def _validate_condition_keys(payload: dict[str, Any]) -> None:
    """文档 10.7：condition 的 key 必须与 tableName 里的表名一致。"""
    cond = payload.get("condition") or {}
    if not cond:
        return
    targets = set(payload.get("tableName") or [])
    if not targets:
        # 没显式传 tableName 时无法本地校验展开结果，放行交给服务端
        return
    unknown = [k for k in cond if k not in targets]
    if unknown:
        raise PlatformError(
            "condition 的 key 必须是物理表名，且需出现在 tableName 中；"
            f"当前多出：{', '.join(unknown)}"
        )


# ---------------- 配置与元信息 ----------------

@app.get("/api/config")
def get_config():
    cfg = cfgmod.load_config()
    return {
        "ok": True,
        "data": {
            "apiBase": cfg["apiBase"],
            "platformBase": cfg["platformBase"],
            "loginUsername": cfg["loginUsername"],
            "username": cfg["username"],
            "openApiKeyMasked": cfgmod.mask_key(cfg["openApiKey"]),
            "hasKey": bool(cfg["openApiKey"]),
            "requestTimeout": cfg["requestTimeout"],
        },
    }


@app.post("/api/config")
def patch_config(patch: ConfigPatch):
    data = patch.model_dump(exclude_none=True)
    cfg = cfgmod.save_config(data)
    TOKEN_CACHE.clear()
    return {"ok": True, "data": {"username": cfg["username"]}}


@app.get("/api/meta")
def meta():
    """页面初始化所需的固定元信息：operator 枚举等。"""
    return {
        "ok": True,
        "data": {
            "operators": [
                {"value": k, "label": OPERATOR_LABELS[k], "noValue": k in NO_VALUE_OPS}
                for k in OPERATORS
            ],
            "tableFields": _effective_table_fields(),
            "zipTtlSeconds": ZIP_TTL_SECONDS,
            "pageSizeMax": 10000,
        },
    }


@app.post("/api/check")
def check_connection():
    """校验配置：能否登录（固定账号）+ Key 是否配置。"""
    cfg = cfgmod.load_config()
    if not cfg["username"]:
        return _fail("尚未填写 username（数据权限用户名）", status=400)
    if not cfg["openApiKey"]:
        return _fail("尚未填写 X-Open-Api-Key（对外接口 Key）", status=400)
    client = DataPlatformClient(cfg)
    try:
        token, fresh = TOKEN_CACHE.get(cfg)
        return {
            "ok": True,
            "data": {
                "loginAccount": cfg["loginUsername"],
                "username": cfg["username"],
                "keyMasked": cfgmod.mask_key(cfg["openApiKey"]),
                "tokenCached": not fresh,
                "tokenPreview": token[:24] + "…",
            },
        }
    except PlatformError as e:
        return _fail(e.message, e.code, status=401 if e.code == 401 else 400)
    finally:
        client.close()


@app.get("/api/options")
def get_options():
    """页面下拉选项：业务域 / 表名候选 / 指标域 / 已上线指标。

    注意：/api/datamap/published 按当前 token 用户过滤，api_data 会拿到空，
    因此表名候选用 /api/datamap/all-tables（不按用户过滤）作为兜底。
    """
    cfg = cfgmod.load_config()
    client = DataPlatformClient(cfg)
    try:
        token, _ = TOKEN_CACHE.get(cfg)
        published = client.list_published_tables(token) or []
        all_tables = []
        if not published:
            try:
                all_tables = client.list_all_tables(token) or []
            except PlatformError:
                all_tables = []
        data = {
            "datamapDomains": client.list_datamap_domains(token) or [],
            "publishedTables": published,
            "allTables": all_tables,
            "metricDomains": client.list_metric_domains(token) or [],
            "publishedMetrics": client.list_published_metrics(token) or [],
        }
        return {"ok": True, "data": data}
    except PlatformError as e:
        return _fail(e.message, e.code, status=400)
    finally:
        client.close()


# ---------------- 核心：拉取 ----------------

@app.post("/api/pull")
def pull(req: PullRequest):
    """按文档流程：登录取 token → pullFile 拿链接 → 立即下载 ZIP → 解析。"""
    cfg = cfgmod.load_config()
    payload = _clean(req.model_dump(exclude_none=True))
    if not payload.get("username"):
        payload["username"] = cfg.get("username")
    if not payload.get("username"):
        return _fail("缺少 username（数据权限用户名）")
    if not cfg.get("openApiKey"):
        return _fail("尚未配置 X-Open-Api-Key")

    # model_dump() 已把 ConditionItem 递归转成 dict，这里直接用
    # 同时剔除 isNull/isNotNull 的空 value，与文档要求一致
    if "condition" in payload:
        payload["condition"] = {
            k: [
                {kk: vv for kk, vv in c.items() if vv != "" or kk == "value"}
                for c in v
            ]
            for k, v in payload["condition"].items()
        }

    started = datetime.now()
    client = DataPlatformClient(cfg)
    try:
        _validate_condition_keys(payload)

        # 1. token（带缓存复用）
        token, _ = TOKEN_CACHE.get(cfg)

        # 2. 拿下载链接
        link = client.pull_file(payload, token=token)
        download_url = link.get("downloadUrl")
        file_name = link.get("fileName")
        if not download_url:
            raise PlatformError("接口未返回 downloadUrl")

        # 3. 立刻下载（文件 3 分钟后清理）
        zip_bytes = client.download_zip(download_url)

        # 4. 解析
        parsed = parse_zip(zip_bytes)
        entries = list_zip_entries(zip_bytes)

        cfgmod._ensure_dir()
        stamp = now_stamp()
        run_dir = cfgmod.DATA_DIR / f"run_{stamp}"
        extract_zip(zip_bytes, run_dir)

        metrics = [
            {**m, "table": flatten_metric_payload(m.get("payload"))}
            for m in parsed["metrics"]
        ]
        combinations = [
            {**c, "table": flatten_metric_payload(c.get("payload"))}
            for c in parsed["combinations"]
        ]

        record = {
            "id": stamp,
            "time": started.strftime("%Y-%m-%d %H:%M:%S"),
            "fileName": file_name,
            "zipSize": len(zip_bytes),
            "datasetCount": len(parsed["datasets"]),
            "totalRows": sum(d["rowCount"] for d in parsed["datasets"]),
            "message": parsed["manifest"].get("message", ""),
        }
        _save_history(record)
        _update_field_cache(parsed["datasets"])

        return {
            "ok": True,
            "data": {
                "runId": stamp,
                "downloadUrl": download_url,
                "fileName": file_name,
                "zipSize": len(zip_bytes),
                "manifest": parsed["manifest"],
                "entries": entries,
                "datasets": parsed["datasets"],
                "metrics": metrics,
                "combinations": combinations,
                "errors": parsed["errors"],
                "savedDir": str(run_dir),
                "elapsedSec": round((datetime.now() - started).total_seconds(), 2),
                "zipTtlNote": f"下载链接对应文件 {ZIP_TTL_SECONDS // 60} 分钟后清理，已立即下载并留存本地",
            },
        }
    except PlatformError as e:
        return _fail(e.message, e.code)
    except Exception as e:
        return _fail(f"解析或下载失败：{e}", status=500)
    finally:
        client.close()


@app.post("/api/pull/sample")
def pull_sample():
    """用本地样本 ZIP 走完整解析链路（数据权限未开通时用于验证效果）。"""
    sample = cfgmod.DATA_DIR / "_sample" / "sample.zip"
    if not sample.exists():
        return _fail("样本不存在，请先运行 tools/make_sample_zip.py", status=404)
    started = datetime.now()
    try:
        zip_bytes = sample.read_bytes()
        parsed = parse_zip(zip_bytes)
        metrics = [
            {**m, "table": flatten_metric_payload(m.get("payload"))}
            for m in parsed["metrics"]
        ]
        combinations = [
            {**c, "table": flatten_metric_payload(c.get("payload"))}
            for c in parsed["combinations"]
        ]
        return {
            "ok": True,
            "data": {
                "downloadUrl": "",
                "fileName": "sample.zip（本地样本，非接口返回）",
                "zipSize": len(zip_bytes),
                "manifest": parsed["manifest"],
                "entries": list_zip_entries(zip_bytes),
                "datasets": parsed["datasets"],
                "metrics": metrics,
                "combinations": combinations,
                "errors": parsed["errors"],
                "savedDir": str(sample.parent),
                "elapsedSec": round((datetime.now() - started).total_seconds(), 2),
                "zipTtlNote": "本地样本，无过期时间",
            },
        }
    except Exception as e:
        return _fail(f"样本解析失败：{e}", status=500)


@app.get("/api/history")
def history():
    return {"ok": True, "data": _load_history()}


# ---------------- BI 看板 ----------------

@app.get("/api/bi/dataset")
def bi_dataset():
    """BI 看板数据集：真实表 → 模板可直接消费的中文表头结构。"""
    try:
        return {"ok": True, "data": bi_dashboard.build_dataset()}
    except FileNotFoundError as e:
        return _fail(str(e), status=404)


@app.get("/api/bi/export.xlsx")
def bi_export_xlsx():
    """下载数据集 Excel（明细 + 口径说明两个工作表）。"""
    try:
        data = bi_dashboard.build_dataset()
    except FileNotFoundError as e:
        return _fail(str(e), status=404)
    content = bi_dashboard.dataset_to_xlsx(data)
    filename = f"服务需求反馈_{data['chineseName'] or data['table']}.xlsx"
    from urllib.parse import quote
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@app.get("/api/bi/template")
def bi_template():
    """返回 BI 看板模板 HTML（前端内嵌渲染用）。"""
    path = cfgmod.BASE_DIR / "frontend" / "public" / "bi-dashboard-template.html"
    if not path.exists():
        return _fail("看板模板文件不存在", status=404)
    return FileResponse(path, media_type="text/html; charset=utf-8")


@app.get("/api/bi/mapping")
def bi_mapping_config():
    """当前生效的字段映射配置（业务方据此核对改表是否生效）。"""
    rows, rules, source = bi_mapping.load_mapping()
    return {
        "ok": True,
        "data": {
            "source": source,
            "path": str(bi_mapping.mapping_path()) if bi_mapping.mapping_path() else "",
            "rows": rows,
            "rules": rules,
        },
    }


@app.get("/api/bi/mapping.xlsx")
def bi_mapping_download():
    """下载字段对应表 Excel（业务方改这个文件）。"""
    from urllib.parse import quote
    path = bi_mapping.mapping_path()
    if not path:
        # 还没有就现场生成一份
        path = bi_mapping.write_template_excel()
    filename = bi_mapping.MAPPING_FILENAME
    return Response(
        content=path.read_bytes(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@app.post("/api/bi/mapping/regenerate")
def bi_mapping_regenerate():
    """按当前真实数据重新生成映射表（刷新候选字段清单与填充率）。"""
    try:
        path = bi_mapping.write_template_excel()
    except Exception as e:
        return _fail(f"生成失败：{e}", status=500)
    return {"ok": True, "data": {"path": str(path), "message": "已按最新数据重新生成映射表"}}


@app.get("/api/bi/export.html")
def bi_export_html():
    """下载带真实数据的自包含看板 HTML（离线可看、可再编辑）。"""
    path = cfgmod.BASE_DIR / "frontend" / "public" / "bi-dashboard-template.html"
    if not path.exists():
        return _fail("看板模板文件不存在", status=404)
    try:
        data = bi_dashboard.build_dataset()
    except FileNotFoundError as e:
        return _fail(str(e), status=404)

    template = path.read_text(encoding="utf-8")

    # 下载版必须离线可用：把 vendor 里的 ECharts 内联进单文件
    vendor = cfgmod.BASE_DIR / "frontend" / "public" / "vendor" / "echarts.min.js"
    if vendor.exists():
        lib = vendor.read_text(encoding="utf-8")
        template = template.replace(
            '<script src="/vendor/echarts.min.js"></script>',
            "<script>\n/* ECharts 5.5.0 (Apache-2.0) 内联 */\n" + lib + "\n</script>",
        )
    else:
        template = template.replace(
            '<script src="/vendor/echarts.min.js"></script>',
            "<script>/* 未找到本地 ECharts，离线打开时图表不可用 */</script>",
        )

    marker = "var RAW_DATA = "
    start = template.find(marker)
    end = template.find("// ======================== COLUMN MAPPING ========================")
    if start < 0 or end < 0:
        return _fail("模板中未找到数据注入位置", status=500)

    payload = json.dumps(data["rows"], ensure_ascii=False).replace("</", "<\\/")
    meta = json.dumps(
        {
            "chineseName": data["chineseName"],
            "rowCount": data["rowCount"],
            "diffAreas": data["diffAreas"],
            "columns": data["columns"],
        },
        ensure_ascii=False,
    ).replace("</", "<\\/")

    html = (
        template[:start]
        + marker + payload + ";\n"
        + "var RAW_META = " + meta + ";\n\n"
        + template[end:]
    )
    filename = f"服务需求BI看板_{data['chineseName'] or data['table']}.html"
    from urllib.parse import quote
    return Response(
        content=html.encode("utf-8"),
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@app.get("/api/history/{run_id}/file")
def history_file(run_id: str, name: str):
    root = cfgmod.DATA_DIR.resolve()
    path = (cfgmod.DATA_DIR / f"run_{run_id}" / name).resolve()
    if not str(path).startswith(str(root)) or not path.exists():
        return _fail("文件不存在", status=404)
    return FileResponse(path)


# ---------------- 供应商指标类接口 ----------------

@app.post("/api/supplier/pass-rate")
def pass_rate(req: PassRateRequest):
    if not req.lifnr.strip():
        return _fail("lifnr（供应商编码）必填")
    cfg = cfgmod.load_config()
    payload = _clean({
        "username": req.username or cfg.get("username"),
        "vdatumStart": req.vdatumStart, "vdatumStop": req.vdatumStop,
        "werk": req.werk, "lifnr": req.lifnr,
    })
    client = DataPlatformClient(cfg)
    try:
        token, _ = TOKEN_CACHE.get(cfg)
        return {"ok": True, "data": client.supplier_pass_rate(payload, token=token)}
    except PlatformError as e:
        return _fail(e.message, e.code)
    finally:
        client.close()


@app.post("/api/supplier/delivery-rate")
def delivery_rate(req: DeliveryRateRequest):
    cfg = cfgmod.load_config()
    payload = _clean({
        "username": req.username or cfg.get("username"),
        "supplierCode": req.supplierCode,
        "startDate": req.startDate, "stopDate": req.stopDate,
        "factory": req.factory,
    })
    client = DataPlatformClient(cfg)
    try:
        token, _ = TOKEN_CACHE.get(cfg)
        return {"ok": True, "data": client.supplier_delivery_rate(payload, token=token)}
    except PlatformError as e:
        return _fail(e.message, e.code)
    finally:
        client.close()


# ---------------- 静态前端 ----------------

FRONTEND_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
if FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")