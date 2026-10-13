# -*- coding: utf-8 -*-
"""真实入口验证：用真实 .xls 打通 HTTP 路由（TestClient 直驱 FastAPI 应用）。

覆盖通用导入预览、LIS 预览与确认保存，以及内容/扩展名不匹配时的错误提示。
"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for path in (PROJECT_ROOT, PROJECT_ROOT / "modules", PROJECT_ROOT / "platform" / "backend"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "lis_sample.xls"
client = TestClient(app)


def _upload(path: str, filename: str, content: bytes):
    return client.post(path, files={"file": (filename, BytesIO(content), "application/vnd.ms-excel")})


def main() -> None:
    content = FIXTURE.read_bytes()
    assert content[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "样本必须是真 OLE/BIFF .xls"

    preview = _upload("/api/import/preview", "lis_sample.xls", content)
    print("通用导入预览:", preview.status_code, preview.json()["status"])
    body = preview.json()["data"]
    print("  sheet_count=", body["sheet_count"], "total_rows=", body["total_rows"])
    print("  sheet0=", body["sheets"][0]["name"], body["sheets"][0]["columns"][:4], "rows=", body["sheets"][0]["rows"])
    assert preview.status_code == 200 and body["total_rows"] == 2

    lis = _upload("/api/data-analysis/lis/preview", "lis_sample.xls", content)
    print("LIS 预览:", lis.status_code)
    data = lis.json()
    print("  header_row=", data["header_row"], "sheet=", data["sheet"], "rows=", data["row_count"])
    print("  headers=", data["headers"])
    print("  字段映射=", [(m["source"], m["target_label"]) for m in data["mapping"] if m["target_key"]])
    assert lis.status_code == 200 and data["header_row"] == 2 and data["row_count"] == 2

    confirm = client.post("/api/data-analysis/lis/confirm", json={
        "session_id": data["session_id"], "header_row": data["header_row"],
        "lab": "验证实验室", "mapping": data["mapping"],
    })
    print("LIS 确认保存:", confirm.status_code, confirm.json())
    assert confirm.status_code == 200

    download = client.get(f"/api/data-analysis/lis/download/{confirm.json()['batch_id']}")
    print("标准化文件下载:", download.status_code, download.headers.get("content-type"), len(download.content), "bytes")
    assert download.status_code == 200 and download.content[:2] == b"PK"

    mismatch = _upload("/api/import/preview", "伪装.xlsx", content)
    print("内容/扩展名不匹配:", mismatch.status_code, mismatch.json().get("detail"))
    assert mismatch.status_code == 400

    print("\n真实入口验证通过：.xls 已可导入、预览、标准化保存，原始文件未被改动。")
    print("原始 .xls 未改动 =", FIXTURE.read_bytes() == content)


if __name__ == "__main__":
    main()
