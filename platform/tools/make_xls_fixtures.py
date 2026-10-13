# -*- coding: utf-8 -*-
"""生成测试用的真实 BIFF .xls 固定样本。

为什么需要：`.xls` 是 OLE/BIFF 二进制格式，不能用 openpyxl 写出；
而把 `.xlsx` 改名成 `.xls` 冒充旧格式，会让「真实 .xls 支持」变成假通过。
所以这里用 xlwt 生成真正的 BIFF8 文件，落盘到 tests/fixtures/ 后入库，
测试运行时只读该样本，不需要 xlwt 参与。

用法：
    python tools/make_xls_fixtures.py
"""
from __future__ import annotations

import os

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "fixtures")


def build_lis_workbook(path: str) -> None:
    """LIS 整理场景：首行标题 + 第 2 行真实表头，覆盖必填标准字段。"""
    import xlwt

    book = xlwt.Workbook(encoding="utf-8")
    sheet = book.add_sheet("LIS导出")
    sheet.write(0, 0, "LIS导出报告")          # 标题行：验证自动表头识别会跳过它
    for col, value in enumerate(["条码号", "姓名", "性别", "检验项目", "检验结果", "接收时间", "报告时间"]):
        sheet.write(1, col, value)
    for offset, value in enumerate([
        ["B001", "张三", "男", "血常规", "正常", "2026-10-01 08:00:00", "2026-10-01 09:00:00"],
        ["B002", "李四", "女", "生化", "5.2", "2026-10-01 09:00:00", "2026-10-01 10:30:00"],
    ]):
        for index, item in enumerate(value):
            sheet.write(offset + 2, index, item)
    book.save(path)


def build_report_workbook(path: str) -> None:
    """通用预览场景：两层合并式表头，验证多级表头拼接。"""
    import xlwt

    book = xlwt.Workbook(encoding="utf-8")
    sheet = book.add_sheet("报告")
    # 第 1 行是分组行（重复值），第 2 行是真实列名：验证预览的多级表头拼接
    sheet.write(0, 0, "基础信息")
    sheet.write(0, 1, "基础信息")
    sheet.write(0, 2, "检测结果")
    sheet.write(0, 3, "检测结果")
    sheet.write(1, 0, "医院名称")
    sheet.write(1, 1, "检测完成时间")
    sheet.write(1, 2, "模块")
    sheet.write(1, 3, "项目名称")
    sheet.write(2, 0, "绵阳市人民医院")
    sheet.write(2, 1, "2026-09-28 09:15")
    sheet.write(2, 2, "M1")
    sheet.write(2, 3, "项目A")
    sheet.write(3, 0, "江油市人民医院")
    sheet.write(3, 1, "2026-09-28 10:15")
    sheet.write(3, 2, "M2")
    sheet.write(3, 3, "项目B")
    book.save(path)


def main() -> None:
    os.makedirs(FIXTURES_DIR, exist_ok=True)
    build_lis_workbook(os.path.join(FIXTURES_DIR, "lis_sample.xls"))
    build_report_workbook(os.path.join(FIXTURES_DIR, "report_multilevel_header.xls"))
    print("generated:", sorted(os.listdir(FIXTURES_DIR)))


if __name__ == "__main__":
    main()
