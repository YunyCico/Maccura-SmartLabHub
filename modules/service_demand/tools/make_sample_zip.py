"""造一个符合《对外数据拉取接口（文件下载版）》8.2 节结构的 ZIP 样本。

结构（与文档完全一致）：
    tables/<name>.columns.json   字段元数据
    tables/<name>.csv            数据
    metrics/<code>.json          指标结果
    combinations/<code>.json     组合结果
    manifest.json                清单

用途：数据权限未开通时，仍能验证解析链路与前端展示效果。
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
DEST = BASE / "data" / "_sample"


def build(with_paging: bool = False) -> Path:
    DEST.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()

    tables = [
        {
            "tableName": "ADS_SERVICE_DEMAND_FEEDBACK",
            "chineseName": "服务需求反馈",
            "columns": [
                ("REQUESTID", "需求单ID", "BIGINT"),
                ("DABH", "单据编号", "VARCHAR(32)"),
                ("YJFL", "一级分类", "VARCHAR(64)"),
                ("EJFL", "二级分类", "VARCHAR(64)"),
                ("ZDKH", "重点客户", "VARCHAR(128)"),
                ("DQ", "地区", "VARCHAR(64)"),
                ("LASTNAME1", "负责人", "VARCHAR(32)"),
                ("DEPARTMENTNAME1", "处理部门", "VARCHAR(128)"),
                ("CREATEDATE", "提交日期", "DATE"),
                ("SCORE", "满意度评分", "DECIMAL(4,2)"),
                ("ISUPGRADE", "是否升级", "VARCHAR(8)"),
                ("REPLYCNT", "响应次数", "INT"),
            ],
            "rows": 14,
        },
        {
            "tableName": "ADS_SATISFACTION_DIM",
            "chineseName": "满意度维度明细",
            "columns": [
                ("HOSPITAL", "医院名称", "VARCHAR(128)"),
                ("DIMNAME", "评价维度", "VARCHAR(64)"),
                ("SCORE", "得分", "DECIMAL(4,2)"),
                ("JOINCNT", "评价人数", "INT"),
                ("PROBLEMCNT", "问题数", "INT"),
            ],
            "rows": 16,
        },
    ]

    dfs = {
        "ADS_SERVICE_DEMAND_FEEDBACK": pd.DataFrame({
            "REQUESTID": list(range(5298479, 5298479 + 14)),
            "DABH": [f"FWXQSQ202610{9:02d}{i:04d}" for i in range(1, 15)],
            "YJFL": ["服务质量需求"] * 9 + ["服务态度需求"] * 5,
            "EJFL": (["授课", "培训", "驻场", "答疑"] * 4)[:14],
            "ZDKH": (["柳林县人民医院", "成都市第三人民医院", "绵阳市中心医院",
                    "南充市中心医院"] * 4)[:14],
            "DQ": (["华东一区", "西南一区", "西南一区", "西南二区"] * 4)[:14],
            "LASTNAME1": (["孟鹏杰", "张伟", "李娜", "王强"] * 4)[:14],
            "DEPARTMENTNAME1": ["实验室运营管理部"] * 14,
            "CREATEDATE": [f"2026-10-{d:02d}" for d in range(1, 15)],
            "SCORE": [4.5, 5, 3, 4, 4.5, 5, 3.5, 4, 5, 4.5, 4, 5, 4.2, 3.8],
            "ISUPGRADE": ["否", "否", "是", "否", "否", "否", "否", "是",
                          "否", "否", "否", "否", "否", "否"],
            "REPLYCNT": [1, 1, 3, 2, 1, 1, 2, 4, 1, 1, 2, 1, 1, 3],
        }),
        "ADS_SATISFACTION_DIM": pd.DataFrame({
            "HOSPITAL": ["柳林县人民医院"] * 8 + ["成都市第三人民医院"] * 8,
            "DIMNAME": ["服务态度", "专业能力", "响应速度", "环境卫生",
                    "设备完好", "流程规范", "培训支持", "投诉处理"] * 2,
            "SCORE": [4.2, 4.5, 3.8, 4.6, 4.4, 4.1, 3.9, 4.3,
                    4.7, 4.4, 4.5, 4.2, 4.6, 4.3, 4.0, 4.5],
            "JOINCNT": [18] * 8 + [25] * 8,
            "PROBLEMCNT": [0, 0, 2, 0, 1, 1, 2, 0, 0, 0, 0, 1, 0, 0, 3, 0],
        }),
    }

    manifest_tables = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for t in tables:
            name = t["tableName"]
            cols_meta = {
                "tableName": name,
                "chineseName": t["chineseName"],
                "columns": [
                    {"fieldName": f, "comment": c, "fieldType": ty, "length": ""}
                    for f, c, ty in t["columns"]
                ],
            }
            z.writestr(
                f"tables/{name}.columns.json",
                json.dumps(cols_meta, ensure_ascii=False, indent=2),
            )
            df = dfs[name]
            if with_paging:
                df = df.head(5)
            z.writestr(f"tables/{name}.csv",
                       df.to_csv(index=False).encode("utf-8-sig"))

            entry = {
                "tableName": name,
                "chineseName": t["chineseName"],
                "columnsFile": f"tables/{name}.columns.json",
                "dataFile": f"tables/{name}.csv",
                "rowCount": int(len(df)),
            }
            if with_paging:
                entry.update({"pageNo": 1, "pageSize": 5,
                              "total": t["rows"], "totalPages": -(-t["rows"] // 5)})
            manifest_tables.append(entry)

        z.writestr("metrics/cb.json", json.dumps([
            {"COMPANY_CODE": "1100", "GJPER": "2026-01", "LEGAL_MONEY": 152340.55},
            {"COMPANY_CODE": "1100", "GJPER": "2026-02", "LEGAL_MONEY": 168902.10},
            {"COMPANY_CODE": "2100", "GJPER": "2026-01", "LEGAL_MONEY": 88230.00},
            {"COMPANY_CODE": "2100", "GJPER": "2026-02", "LEGAL_MONEY": 91340.78},
        ], ensure_ascii=False, indent=2))

        z.writestr("combinations/sales_dashboard.json", json.dumps({
            "dimensions": ["COMPANY_CODE", "GJPER"],
            "rows": [
                {"COMPANY_CODE": "1100", "GJPER": "2026-01",
                 "totalCost": 152340.55, "orderCnt": 128},
                {"COMPANY_CODE": "1100", "GJPER": "2026-02",
                 "totalCost": 168902.10, "orderCnt": 141},
            ],
        }, ensure_ascii=False, indent=2))

        z.writestr("manifest.json", json.dumps({
            "tables": manifest_tables,
            "metrics": [{
                "metricId": 1, "metricCode": "cb", "metricName": "成本",
                "file": "metrics/cb.json",
            }],
            "combinations": [{
                "combinationId": 1, "combinationCode": "sales_dashboard",
                "combinationName": "销售看板",
                "file": "combinations/sales_dashboard.json",
            }],
            "message": (
                f"表: 请求 {len(tables)}, 成功返回 {len(tables)} | "
                "指标: 请求 1, 成功返回 1 | 组合: 请求 1, 成功返回 1"
            ),
        }, ensure_ascii=False))

    out = DEST / "sample.zip"
    out.write_bytes(buf.getvalue())
    return out


if __name__ == "__main__":
    p = build()
    print(f"样本已生成: {p} ({p.stat().st_size} bytes)")