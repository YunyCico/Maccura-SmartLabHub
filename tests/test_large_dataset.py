"""大表模式回归：流式导入 SQLite、分页预览、聚合、编辑封锁。全部 tmp 隔离。"""
import openpyxl
import pytest

from smartlab_core import smartlab_engine as engine


@pytest.fixture
def large_env(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    (lib / "sources").mkdir(parents=True)
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(engine, "LIB_DIR", str(lib))
    monkeypatch.setattr(engine, "OUT_DIR", str(out))
    monkeypatch.setattr(engine, "BAK_DIR", str(out / "_bak"))
    monkeypatch.setattr(engine, "STATE", {"datasets": [], "config": {}})
    monkeypatch.setattr(engine, "LARGE_FILE_BYTES", 1)   # 任意文件都走大表模式
    engine._cache.clear()
    return lib


def _make(path, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(rows[0])
    for r in rows[1:]:
        ws.append(r)
    wb.save(str(path))


def test_large_import_sqlite_meta_and_preview(large_env, tmp_path):
    src = tmp_path / "big.xlsx"
    _make(src, [
        ["实验室", "模块", "模块", "值"],
        ["L1", "M1", None, "1"],
        ["L2", "M2", None, "2"],
        ["L3", "M3", None, "3"],
    ])
    ds = engine.import_file(str(src), copy=True)
    assert ds["large"] is True
    sh = ds["sheets"][0]
    assert sh["large"] is True
    import os
    assert os.path.exists(engine._ds_db_path(ds))
    # 重复空表头列被丢，列 = 实验室/模块/值
    assert sh["base_columns"] == ["实验室", "模块", "值"]

    # 分页预览
    page0 = engine._large_page(ds, sh, 0, 2)
    page1 = engine._large_page(ds, sh, 2, 2)
    assert len(page0) == 2 and len(page1) == 1
    assert page0[0][0] == "L1" and page1[0][0] == "L3"


def test_large_aggregate_union_and_cap(large_env, tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "MAX_RESULT_ROWS", 2)   # 验证兜底封顶
    for i, name in enumerate(["a.xlsx", "b.xlsx"]):
        src = tmp_path / name
        _make(src, [
            ["实验室", "模块", "模块", "值"],
            [f"L{i}1", "M1", None, str(i)],
            [f"L{i}2", "M2", None, str(i + 1)],
        ])
        engine.import_file(str(src), copy=True)

    req = {"sources": [{"dataset_id": d["id"], "sheet": "Sheet1"} for d in engine.STATE["datasets"]],
           "fields": ["实验室", "模块", "值"], "add_src": True}
    by_id = {d["id"]: d for d in engine.STATE["datasets"]}
    r = engine.aggregate_union(req, by_id, ["实验室", "模块", "value_x"], [], True, False, {}, 0)
    # 封顶 2 行且带截断标记
    assert len(r["rows"]) == 2 and r["truncated"] is True
    assert r["columns"][0:3] == ["来源实验室", "来源文件", "来源工作表"]
    assert all(x["large"] for x in r["detail"])


def test_large_edit_blocked(large_env, tmp_path):
    src = tmp_path / "big.xlsx"
    _make(src, [["实验室", "模块"], ["L1", "M1"]])
    ds = engine.import_file(str(src), copy=True)
    with pytest.raises(ValueError, match="大表暂不支持"):
        engine.raw_edit_apply(ds["id"], "Sheet1", [{"t": "row", "r": 1}])
    with pytest.raises(ValueError, match="大表暂不支持"):
        engine.raw_edit_save(ds["id"], "Sheet1")
