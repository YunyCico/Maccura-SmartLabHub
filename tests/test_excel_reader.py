from io import BytesIO

from openpyxl import Workbook

from smartlab_core.excel_reader import read_excel_preview


def workbook_bytes(rows: list[list[object]], sheet_name: str = "数据") -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_name
    for row in rows:
        sheet.append(row)
    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def test_preview_joins_multi_level_headers_and_detects_report_fields() -> None:
    content = workbook_bytes(
        [
            ["基础信息", "基础信息", "检测结果", "检测结果"],
            ["医院名称", "检测完成时间", "模块", "项目名称"],
            ["绵阳市人民医院", "2026-09-28 09:15", "M1", "项目A"],
            ["江油市人民医院", "2026-09-28 10:15", "M2", "项目B"],
        ]
    )

    result = read_excel_preview(content, "测试多层表头.xlsx")
    sheet = result["sheets"][0]

    assert result["total_rows"] == 2
    assert sheet["header_rows"] == "1,2"
    assert sheet["columns"] == [
        "基础信息 / 医院名称",
        "基础信息 / 检测完成时间",
        "检测结果 / 模块",
        "检测结果 / 项目名称",
    ]
    assert sheet["detected_fields"]["time"] == "基础信息 / 检测完成时间"
    assert sheet["detected_fields"]["module"] == "检测结果 / 模块"
    assert sheet["detected_fields"]["project"] == "检测结果 / 项目名称"
    assert sheet["warnings"] == []


def test_preview_recognizes_key_value_info_table() -> None:
    content = workbook_bytes(
        [
            ["填报日期", "2026-09-28"],
            ["医院名称", "绵阳市人民医院"],
            ["负责人", "张三"],
        ],
        sheet_name="填报信息",
    )

    result = read_excel_preview(content, "信息表.xlsx")
    sheet = result["sheets"][0]

    assert sheet["kind"] == "info"
    assert sheet["columns"] == ["项目", "内容"]
    assert sheet["rows"] == 3
    assert sheet["preview"][1]["项目"] == "医院名称"
    assert sheet["preview"][1]["内容"] == "绵阳市人民医院"
