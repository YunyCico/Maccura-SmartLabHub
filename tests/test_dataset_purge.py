"""删除数据表时，源文件副本与大表 SQLite 必须一起清掉。

★ 回归背景（三个洞）：
    1. 「清空全部」只删源文件 → 大表 SQLite 全成孤儿（几百 MB 白占磁盘）
    2. FastAPI 侧 dataset_clear 什么都不删，只清索引
    3. 单删只删第一个子表的 db → 17 个子表留下 16 个孤儿

全部 tmp 隔离，绝不碰用户真实数据。
"""
import json
import os
import threading
import time
import urllib.request

import openpyxl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.data_analysis import router
from modules.data_analysis import smartlab_engine as engine


@pytest.fixture
def purge_env(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    (lib / "sources").mkdir(parents=True)
    (lib / "db").mkdir(parents=True)
    monkeypatch.setattr(engine, "LIB_DIR", str(lib))
    monkeypatch.setattr(engine, "SRC_DIR", str(lib / "sources"))
    monkeypatch.setattr(engine, "OUT_DIR", str(tmp_path / "out"))
    monkeypatch.setattr(engine, "BAK_DIR", str(tmp_path / "out" / "_bak"))
    monkeypatch.setattr(engine, "STATE", {
        "datasets": [],
        # 用完整的默认配置 —— 缺 "ai" 之类的键会让 get_config() 直接 KeyError
        "config": json.loads(json.dumps(engine.DEFAULT_CONFIG)),
    })
    monkeypatch.setattr(engine, "LARGE_FILE_BYTES", 1)      # 任何文件都走大表模式
    engine._cache.clear()
    return lib


def _make(path, sheets=3):
    wb = openpyxl.Workbook()
    for index in range(sheets):
        ws = wb.active if index == 0 else wb.create_sheet()
        ws.title = "表%d" % index
        ws.append(["项目", "值"])
        for row in range(5):
            ws.append(["行%d" % row, str(row)])
    wb.save(str(path))


def _leftovers(lib):
    dbs = list((lib / "db").glob("*.db"))
    srcs = list((lib / "sources").glob("*.xlsx"))
    return dbs, srcs


def test_delete_dataset_removes_db_and_source(purge_env, tmp_path):
    """单删一个数据集：它的所有大表库和源文件副本都要消失。"""
    src = tmp_path / "a.xlsx"
    _make(src)
    ds = engine.import_file(str(src), copy=True)
    dbs, srcs = _leftovers(purge_env)
    assert len(dbs) == 3, "应有 3 个大表库"
    assert len(srcs) == 1

    app = FastAPI()
    app.include_router(router, prefix="/api")
    client = TestClient(app)
    response = client.post("/api/data-analysis/dataset/delete", json={"ids": [ds["id"]]})
    assert response.status_code == 200, response.text

    dbs, srcs = _leftovers(purge_env)
    assert not dbs, "大表库没清干净：%s" % [p.name for p in dbs]
    assert not srcs, "源文件副本没清：%s" % [p.name for p in srcs]


def test_clear_all_removes_db_and_source(purge_env, tmp_path):
    """「清空全部」：源文件副本和大表库都要清掉（以前只删源文件）。"""
    src = tmp_path / "a.xlsx"
    _make(src)
    engine.import_file(str(src), copy=True)
    dbs, srcs = _leftovers(purge_env)
    assert len(dbs) == 3 and len(srcs) == 1

    app = FastAPI()
    app.include_router(router, prefix="/api")
    client = TestClient(app)
    response = client.post("/api/data-analysis/dataset/clear", json={})
    assert response.status_code == 200, response.text

    dbs, srcs = _leftovers(purge_env)
    assert not dbs, "大表库没清干净：%s" % [p.name for p in dbs]
    assert not srcs, "源文件副本没清：%s" % [p.name for p in srcs]


def test_engine_clear_all_removes_db_and_source(purge_env, tmp_path):
    """原生引擎的「清空全部」端点同样要清干净。"""
    src = tmp_path / "a.xlsx"
    _make(src)
    engine.import_file(str(src), copy=True)
    assert len(_leftovers(purge_env)[0]) == 3

    port = engine.pick_port(8802)
    server = engine.ThreadingHTTPServer(("127.0.0.1", port), engine.Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.5)
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(
            "http://127.0.0.1:%d/api/dataset/clear" % port,
            data=b"{}", method="POST")
        request.add_header("Content-Type", "application/json")
        with opener.open(request, timeout=60) as resp:
            assert json.loads(resp.read().decode("utf-8")).get("ok") is True
    finally:
        server.shutdown()

    dbs, srcs = _leftovers(purge_env)
    assert not dbs, "大表库没清干净：%s" % [p.name for p in dbs]
    assert not srcs, "源文件副本没清：%s" % [p.name for p in srcs]


def test_delete_closes_cached_workbook_before_removing_source(purge_env, tmp_path):
    """删源文件前必须先关掉缓存的 workbook。

    回归 bug（真实环境实测）：用户删除一个数据集后，db 删干净了，
    源文件副本却留在磁盘上白占 66MB —— 因为 read_only 模式的 workbook
    一直占着 zip 句柄，Windows 上文件被占着 os.remove 直接失败，
    而错误被 `except: pass` 静默吞了。
    """
    src = tmp_path / "a.xlsx"
    _make(src, sheets=1)
    ds = engine.import_file(str(src), copy=True)
    stored = engine.ds_abs_path(ds)
    assert os.path.isfile(stored)

    # 模拟界面预览：读一次原始表 → workbook 进缓存、句柄被占住
    engine.cached_raw(stored, "表0")
    assert stored in [key[0] for key in engine._wb_cache], "workbook 应已被缓存"

    app = FastAPI()
    app.include_router(router, prefix="/api")
    client = TestClient(app)
    response = client.post("/api/data-analysis/dataset/delete", json={"ids": [ds["id"]]})
    assert response.status_code == 200, response.text
    assert not os.path.isfile(stored), "源文件副本没删掉（句柄没释放？）"
    dbs, srcs = _leftovers(purge_env)
    assert not dbs and not srcs


def test_delete_closes_cached_workbook_before_removing_source(purge_env, tmp_path):
    """删源文件前必须先关掉缓存的 workbook。

    回归 bug（真实环境实测）：用户删除一个数据集后，db 删干净了，
    源文件副本却留在磁盘上白占 66MB —— 因为 read_only 模式的 workbook
    一直占着 zip 句柄，Windows 上文件被占着 os.remove 直接失败，
    而错误被 `except: pass` 静默吞了。
    """
    src = tmp_path / "a.xlsx"
    _make(src, sheets=1)
    ds = engine.import_file(str(src), copy=True)
    stored = engine.ds_abs_path(ds)
    assert os.path.isfile(stored)

    # 模拟界面预览：读一次原始表 → workbook 进缓存、句柄被占住
    engine.cached_raw(stored, "表0")
    assert stored in [key[0] for key in engine._wb_cache], "workbook 应已被缓存"

    app = FastAPI()
    app.include_router(router, prefix="/api")
    client = TestClient(app)
    response = client.post("/api/data-analysis/dataset/delete", json={"ids": [ds["id"]]})
    assert response.status_code == 200, response.text
    assert not os.path.isfile(stored), "源文件副本没删掉（句柄没释放？）"
    dbs, srcs = _leftovers(purge_env)
    assert not dbs and not srcs
