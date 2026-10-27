"""报告表格渲染：表头列数必须与数据行单元格数一致。

★ 回归 bug（2026-10-26 用户报「设备负载报告里的表格全是乱的」）：
  很多调用点构造 cell 时自己写了 ``<td>…</td>``，而 ``table()`` 又包了一层，
  于是渲染出 ``<td><td>…</td></td>``。浏览器遇到 td 里的新 td 会
  **隐式关闭当前单元格**，数据行实际渲染出的格子数是表头的两倍
  （实测「逐日对比」表头 9 列、数据渲染成 18 格）→ 整片横向错位。
"""
import re

import pytest

from modules.data_analysis import report_render as RR


def _tables(html):
    """把 HTML 拆成 (表头列数, 各数据行单元格数) 列表。"""
    out = []
    for table in re.findall(r"<table[^>]*>.*?</table>", html, re.S):
        rows = re.findall(r"<tr>.*?</tr>", table, re.S)
        if not rows:
            continue
        n_head = len(re.findall(r"<th", rows[0]))
        if not n_head:
            continue
        widths = {len(re.findall(r"<td", row)) for row in rows[1:]}
        out.append((n_head, widths))
    return out


def test_table_accepts_cells_with_and_without_td():
    """两种 cell 写法都要渲染成"一格一格"的，不能出现双层 td。"""
    html = RR.table(
        ["甲", "乙"],
        [
            ["<td><b>1</b></td>", "<td>2</td>"],      # 调用方自带 td
            ["<b>3</b>", "<i>4</i>"],                 # 只放内容
        ])
    assert "<td><td>" not in html, "出现了双层 td：%s" % html
    assert html.count("<td>") == 4, html
    tables = _tables(html)
    assert tables[0][0] == 2
    assert tables[0][1] == {2}, "数据行格子数应与表头一致：%s" % tables


def test_table_pads_or_trims_to_header_width():
    """列数对不上时补齐/截断，宁可丢内容也不能让整张表错位。"""
    html = RR.table(["甲", "乙", "丙"], [["<td>1</td>"]])
    tables = _tables(html)
    assert tables[0][0] == 3
    assert tables[0][1] == {3}, tables


def test_equip_load_report_tables_are_aligned():
    """设备负载报告里每张表，表头列数 == 数据行格子数。"""
    # 造一份多天的小数据：时间 / 模块 / 项目 三个维度
    from datetime import datetime, timedelta

    rows = []
    base = datetime(2026, 7, 1, 8, 0)
    for day in range(3):
        for slot in range(4):
            for module in ("MAX", "MIX"):
                for _ in range(3 + slot):
                    rows.append([
                        (base + timedelta(days=day, hours=slot)).strftime("%Y-%m-%d %H:%M:%S"),
                        module, "谷丙转氨酶"])
    columns = ["时间", "模块", "项目"]
    from modules.data_analysis import report_templates as RT

    built = RT.build(columns, rows, "equip_load", {})
    html = RR.render_equip_load(built)
    tables = _tables(html)
    assert tables, "报告里应该至少有一张表"
    for n_head, widths in tables:
        assert widths == {n_head}, "表头 %d 列但数据行有 %s 格" % (n_head, sorted(widths))
