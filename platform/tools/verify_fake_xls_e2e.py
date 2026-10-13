# -*- coding: utf-8 -*-
"""真实用户的「假 .xls」文件端到端验证（TestClient 直驱 FastAPI）。

覆盖用户实际遇到的那批文件：扩展名 .xls，内容分别是 CSV / HTML / 真 BIFF。
用法：python tools/verify_fake_xls_e2e.py
样本路径可用环境变量 SLH_LIS_DIR 覆盖。
"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
import os
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for path in (PROJECT_ROOT, PROJECT_ROOT / "modules", PROJECT_ROOT / "platform" / "backend"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

LIS_DIR = Path(os.environ.get("SLH_LIS_DIR", r"C:\Users\98319\Desktop\LIS数据表单"))

CANDIDATES = [
    ("爱成/LIS411真xls", LIS_DIR / "爱成" / "25.9月生化LIS411 总汇表 result.xls"),
    ("杨娜/CSV伪xls", LIS_DIR / "杨娜" / "table_2.xls"),
    ("杨娜/仪器工作量HTML伪xls", LIS_DIR / "杨娜" / "导出仪器工作量26年6-7月.xls"),
    ("杨娜/科室工作量HTML伪xls", LIS_DIR / "杨娜" / "导出科室工作量26年6-7月.xls"),
]

client = TestClient(app)


def main() -> None:
    if not LIS_DIR.is_dir():
        print("SKIP: 找不到 LIS 样例目录", LIS_DIR)
        return

    failures = []
    for label, path in CANDIDATES:
        if not path.is_file():
            print(f"SKIP {label}: 文件不存在 {path}")
            continue
        content = path.read_bytes()
        head = content[:8].hex()
        res = client.post(
            "/api/import/preview",
            files={"file": (path.name, BytesIO(content), "application/vnd.ms-excel")},
        )
        if res.status_code != 200:
            failures.append(f"{label}: HTTP {res.status_code} {res.text[:160]}")
            print(f"FAIL {label}: {res.status_code} {res.text[:160]}")
            continue
        body = res.json()["data"]
        columns = body["sheets"][0]["columns"]
        print(f"OK   {label}")
        print(f"     magic={head} sheets={body['sheet_count']} rows={body['total_rows']} cols={len(columns)}")
        print(f"     note={body.get('format_note')}")
        print(f"     columns={columns[:5]}")

        lis = client.post(
            "/api/data-analysis/lis/preview",
            files={"file": (path.name, BytesIO(content), "application/vnd.ms-excel")},
        )
        print(f"     LIS 预览 status={lis.status_code}")
        if lis.status_code == 200:
            print(f"     LIS 表头行={lis.json()['header_row']} 数据行={lis.json()['row_count']}")
        else:
            print(f"     LIS detail={lis.text[:140]}")

    print()
    if failures:
        print("RESULT: FAIL")
        for item in failures:
            print("  -", item)
        raise SystemExit(1)
    print("RESULT: PASS — 扩展名为 .xls 的 CSV / HTML / 真 xls 全部可导入")


if __name__ == "__main__":
    main()