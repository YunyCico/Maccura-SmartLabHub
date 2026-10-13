from io import BytesIO
from pathlib import Path

from openpyxl import Workbook

from modules.data_analysis.excel_reader import read_excel_preview


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
    assert sheet["detected_business_fields"] == {
        "hospital": "基础信息 / 医院名称",
        "time": "基础信息 / 检测完成时间",
    }
    assert sheet["warnings"] == []


FIXTURES = Path(__file__).resolve().parent / "fixtures"
XLS_SAMPLE = FIXTURES / "report_multilevel_header.xls"


def test_preview_reads_real_biff_xls_file() -> None:
    assert XLS_SAMPLE.is_file(), f"缺少真实 XLS 测试样例：{XLS_SAMPLE}"
    assert XLS_SAMPLE.read_bytes()[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "样例必须是 OLE/BIFF 真 .xls"

    result = read_excel_preview(XLS_SAMPLE.read_bytes(), XLS_SAMPLE.name)

    sheet = result["sheets"][0]
    assert result["sheet_count"] == 1
    assert result["total_rows"] == 2
    assert sheet["name"] == "报告"
    assert sheet["columns"] == [
        "基础信息 / 医院名称",
        "基础信息 / 检测完成时间",
        "检测结果 / 模块",
        "检测结果 / 项目名称",
    ]
    assert sheet["preview"][0]["基础信息 / 医院名称"] == "绵阳市人民医院"


def test_preview_reads_multi_sheet_xls() -> None:
    sample = FIXTURES / "lis_sample.xls"
    assert sample.is_file(), f"缺少真实 XLS 测试样例：{sample}"

    result = read_excel_preview(sample.read_bytes(), sample.name)

    assert result["sheet_count"] == 1
    assert result["sheets"][0]["name"] == "LIS导出"
    assert result["sheets"][0]["columns"] == ["条码号", "姓名", "性别", "检验项目", "检验结果", "接收时间", "报告时间"]


def test_preview_reads_by_content_regardless_of_extension() -> None:
    """扩展名与内容不符时，按**内容**读，而不是拒绝。

    这是 2026-10-10 的修复口径：LIS 系统导出时经常把 CSV/HTML 存成 `.xls`，
    甚至把真 xls 存成 `.xlsx`。只要内容认得出来，就该读进来。
    """
    assert XLS_SAMPLE.is_file(), f"缺少真实 XLS 测试样例：{XLS_SAMPLE}"
    content = XLS_SAMPLE.read_bytes()

    renamed = read_excel_preview(content, "其实是xls.xlsx")
    honest = read_excel_preview(content, XLS_SAMPLE.name)

    assert renamed["sheets"] == honest["sheets"], "改名后应按真实内容解析，结果不变"
    assert "格式" in renamed["format_note"] or "读取" in renamed["format_note"]


def test_preview_rejects_unknown_extension() -> None:
    try:
        read_excel_preview(XLS_SAMPLE.read_bytes(), "数据.pdf")
    except ValueError as error:
        assert "仅支持" in str(error)
    else:
        raise AssertionError("未支持的扩展名应被拒绝")


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
