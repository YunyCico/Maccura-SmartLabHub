from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.main import app


def excel_bytes() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["医院名称", "检测完成时间", "模块", "项目名称"])
    sheet.append(["测试医院", "2026-09-28 09:15", "M1", "项目A"])
    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def test_import_preview_does_not_write_file() -> None:
    client = TestClient(app)
    response = client.post(
        "/api/import/preview",
        files={"file": ("临时测试.xlsx", excel_bytes(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "preview_ready"
    assert body["data"]["total_rows"] == 1
    assert body["data"]["sheets"][0]["detected_fields"]["module"] == "模块"


def test_dashboard_summary_recovers_after_database_file_is_removed() -> None:
    database_path = Path(__file__).resolve().parents[1] / "backend" / "department_platform.db"
    database_path.unlink(missing_ok=True)

    client = TestClient(app)
    response = client.get("/api/dashboard/summary")

    assert response.status_code == 200
    assert response.json()["recent_tasks"] == []
