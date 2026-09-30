"""表头去重回归：重复表头且整列无数据 = 模板残留，解析时丢弃；有数据的重复列保留 _2。"""
import openpyxl

from smartlab_core import smartlab_engine as engine


def _write(path, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    for r in rows:
        ws.append(r)
    wb.save(str(path))


def test_empty_duplicate_header_column_is_dropped(tmp_path):
    p = tmp_path / "dup_empty.xlsx"
    _write(p, [
        ["实验室", "模块", "模块", "值"],
        ["L1", "M1", None, 1],
        ["L2", "M2", None, 2],
    ])
    t = engine.build_table(str(p), "Sheet1", 1)
    assert t["base_columns"] == ["实验室", "模块", "值"]
    assert all(not str(c).endswith("_2") for c in t["base_columns"])


def test_duplicate_header_with_data_keeps_suffix(tmp_path):
    p = tmp_path / "dup_data.xlsx"
    _write(p, [
        ["实验室", "模块", "模块", "值"],
        ["L1", "M1", "X1", 1],
        ["L2", "M2", "X2", 2],
    ])
    t = engine.build_table(str(p), "Sheet1", 1)
    assert t["base_columns"] == ["实验室", "模块", "模块_2", "值"]
