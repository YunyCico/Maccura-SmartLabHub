# -*- coding: utf-8 -*-
"""验证：在「数据源管理」里删除表格时，db 文件和源文件副本是否一起清掉。

★ 全程隔离在临时目录（SLH_DATA_DIR），不会碰用户真实数据。

覆盖四条删除路径：
    1. 引擎侧 /api/dataset/delete       （原生页面的删除按钮）
    2. 引擎侧 /api/dataset/clear        （原生页面的「清空全部」）
    3. FastAPI /api/data-analysis/dataset/delete
    4. FastAPI /api/data-analysis/dataset/clear

★ 回归背景：
    - 「清空全部」以前只删源文件副本，大表 SQLite 全成孤儿（几百 MB 白占磁盘）
    - FastAPI 侧的 dataset_clear 以前**什么都不删**，只清索引
    - 单删以前只删第一个子表的 db，17 个子表留下 16 个孤儿
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = PROJECT_ROOT / "platform" / "backend"
for path in (PROJECT_ROOT, BACKEND_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

# ★ 必须在 import 引擎之前设：把数据目录整体重定向到临时目录
_TMP = tempfile.mkdtemp(prefix="slh_purge_")
os.environ["SLH_DATA_DIR"] = _TMP

import openpyxl  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api.data_analysis import router  # noqa: E402
from modules.data_analysis import smartlab_engine as engine  # noqa: E402

engine.LARGE_FILE_BYTES = 1          # 任何文件都走大表模式（保证会生成 db）
engine.load_state()

FAILURES: list[str] = []


def make_xlsx(path: Path, sheets: int = 3) -> None:
    wb = openpyxl.Workbook()
    for index in range(sheets):
        ws = wb.active if index == 0 else wb.create_sheet()
        ws.title = "表%d" % index
        ws.append(["项目", "值"])
        for row in range(5):
            ws.append(["行%d" % row, str(row)])
    wb.save(str(path))


def snapshot() -> tuple[list[str], list[str]]:
    db_dir = Path(engine.LIB_DIR) / "db"
    src_dir = Path(engine.SRC_DIR)
    dbs = sorted(p.name for p in db_dir.glob("*.db")) if db_dir.is_dir() else []
    srcs = sorted(p.name for p in src_dir.glob("*.xlsx")) if src_dir.is_dir() else []
    return dbs, srcs


def check(label: str, dbs: list[str], srcs: list[str], expect_empty: bool = True) -> None:
    ok = (not dbs) and (not srcs) if expect_empty else bool(dbs)
    print("  %s %-46s db=%d 源副本=%d" % ("✅" if ok else "❌", label, len(dbs), len(srcs)))
    if not ok:
        FAILURES.append("%s：db=%d 源副本=%d" % (label, len(dbs), len(srcs)))


def prepare(name: str) -> str:
    """造一个文件并导入，返回 dataset_id。"""
    src = Path(_TMP) / name
    make_xlsx(src)
    ds = engine.import_file(str(src), copy=True)
    return ds["id"]


def main() -> int:
    print("=" * 78)
    print("删除数据表时，db 与源文件副本是否一起清理")
    print("隔离目录：%s" % _TMP)
    print("=" * 78)

    # ---------- 1) FastAPI 单删 ----------
    print("\n[1] FastAPI  /api/data-analysis/dataset/delete")
    ds_id = prepare("del_api.xlsx")
    dbs, srcs = snapshot()
    print("  导入后：db=%d 源副本=%d" % (len(dbs), len(srcs)))
    app = FastAPI()
    app.include_router(router, prefix="/api")
    client = TestClient(app)
    response = client.post("/api/data-analysis/dataset/delete", json={"ids": [ds_id]})
    assert response.status_code == 200, response.text
    check("删除后", *snapshot())

    # ---------- 2) FastAPI 清空全部 ----------
    print("\n[2] FastAPI  /api/data-analysis/dataset/clear")
    prepare("clear_api.xlsx")
    print("  导入后：db=%d 源副本=%d" % (len(snapshot()[0]), len(snapshot()[1])))
    response = client.post("/api/data-analysis/dataset/clear", json={})
    assert response.status_code == 200, response.text
    check("清空后", *snapshot())

    # ---------- 3) 引擎侧 HTTP：单删 ----------
    print("\n[3] 引擎  /api/dataset/delete")
    ds_id = prepare("del_engine.xlsx")
    port = engine.pick_port(8801)
    server = engine.ThreadingHTTPServer(("127.0.0.1", port), engine.Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.5)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        request = urllib.request.Request(
            "http://127.0.0.1:%d/api/dataset/delete" % port,
            data=json.dumps({"ids": [ds_id]}).encode("utf-8"), method="POST")
        request.add_header("Content-Type", "application/json")
        with opener.open(request, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        assert body.get("ok") is True, body
        check("删除后", *snapshot())

        # ---------- 4) 引擎侧 HTTP：清空全部 ----------
        print("\n[4] 引擎  /api/dataset/clear")
        prepare("clear_engine.xlsx")
        print("  导入后：db=%d 源副本=%d" % (len(snapshot()[0]), len(snapshot()[1])))
        request = urllib.request.Request(
            "http://127.0.0.1:%d/api/dataset/clear" % port,
            data=b"{}", method="POST")
        request.add_header("Content-Type", "application/json")
        with opener.open(request, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        assert body.get("ok") is True, body
        check("清空后", *snapshot())
    finally:
        server.shutdown()

    print("\n" + ("全部通过 ✅" if not FAILURES else "存在失败 ❌\n  " + "\n  ".join(FAILURES)))
    shutil.rmtree(_TMP, ignore_errors=True)
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    raise SystemExit(main())
