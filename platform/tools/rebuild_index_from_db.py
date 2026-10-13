# -*- coding: utf-8 -*-
"""从 library/db 缓存重建 sources 源文件，并恢复 index.json 的 datasets。

背景：library/sources 里的原始文件副本丢失（索引也被清空），
但 library/db/*.db 里的 SQLite 缓存仍完整保存着表头与全部数据行。
本脚本把每个 db 反向写成 xlsx（首行 = 表头），再走引擎标准 import_file()
重新入库，这样得到的索引与正常导入完全一致，不靠手写元数据。

用法：python tools/rebuild_index_from_db.py --dry-run
      python tools/rebuild_index_from_db.py
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]      # 项目根（platform/tools -> platform -> 项目根）
for _p in (PROJECT_ROOT, PROJECT_ROOT / "modules"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from modules.data_analysis import smartlab_engine as engine  # noqa: E402

DB_DIR = Path(engine.LIB_DIR) / "db"
WORK_DIR = PROJECT_ROOT / "backup" / "_migration" / "rebuilt_sources"

# ★ 只恢复这一份固定清单里的 db。
#   不能直接扫 db 目录：import_file() 入库时会按新数据集 id 再生成一份 db 缓存，
#   脚本重跑就会把"自己上一轮的产物"当成新数据再导一遍，越滚越多（已踩过）。
#   下面是 sources 目录丢失之前就存在的原始 db，对应最初那批数据。
ORIGINAL_DBS = [
    "ds_0054293337_Sheet1.db",
    "ds_2267f5a43f_Sheet1.db",
    "ds_38829ebb3f_Sheet1.db",
    "ds_38829ebb3f___10_1-12_11.db",
    "ds_83a35610c7_Sheet1.db",
    "ds_a2ba7001a5___2_1-3_31.db",
    "ds_f5a46e61e8_Sheet1.db",
    # ds_10116e08d1_CSV.db 是 0 字节空文件，没有可恢复内容
]

# db 文件名 -> 原工作表名。引擎存储时用 "ds_<id>_<sheet名>.db"，
# sheet 名里的非法文件名字符会被替换过，这里只做展示用。
DS_ID_RE = re.compile(r"^ds_([0-9a-f]{10})_(.+)\.db$")
# 还原出来的源文件是 .xlsx，解析规则必须同时覆盖 db 与还原文件两种后缀。
REBUILT_RE = re.compile(r"^ds_([0-9a-f]{10})_(.+)\.xlsx$")


def sheet_label(path: Path) -> tuple[str, str]:
    """返回 (数据集 id, 工作表名)。后缀不限，db 与还原出的源文件都能解析。"""
    m = REBUILT_RE.match(path.name) or DS_ID_RE.match(path.name)
    if not m:
        return "", path.stem
    ds_id, sheet = m.group(1), m.group(2)
    # 引擎用 "___" 连接 sheet 名里的特殊字符（见 _import_sheet_stream）
    sheet = sheet.replace("___", "-")
    return "ds_" + ds_id, sheet


def dump_db(db_path: Path, out_xlsx: Path) -> dict:
    """把一个 db 缓存写成 xlsx。返回统计信息。"""
    from openpyxl import Workbook

    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        tables = [r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        if not tables:
            return {"rows": 0, "cols": 0, "skipped": "无表"}
        table = tables[0]
        ncols = len(cur.execute(f'SELECT * FROM "{table}" LIMIT 1').description or [])
        if not ncols:
            return {"rows": 0, "cols": 0, "skipped": "无列"}

        wb = Workbook(write_only=True)
        ws = wb.create_sheet("Sheet1")
        rows_written = 0
        header_written = False
        for record in cur.execute(f'SELECT * FROM "{table}"'):
            if not header_written:
                # db 第一行存的就是表头
                ws.append([("" if v is None else str(v)) for v in record])
                header_written = True
                continue
            ws.append([("" if v is None else str(v)) for v in record])
            rows_written += 1
        wb.save(str(out_xlsx))
        return {"rows": rows_written, "cols": ncols, "skipped": ""}
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not DB_DIR.is_dir():
        print("[SKIP] db 目录不存在:", DB_DIR)
        return 0

    dbs = [DB_DIR / name for name in ORIGINAL_DBS]
    missing = [d.name for d in dbs if not d.is_file()]
    rebuilt: list[tuple[Path, str]] = []

    if missing and len(missing) == len(ORIGINAL_DBS) and WORK_DIR.is_dir():
        # 原始 db 已不存在，但还原出来的 xlsx 还在 —— 直接用它们恢复，
        # 不必再从 db 反写一遍（那一步对 54 万行要跑好几分钟）。
        print(f"[INFO] 原始 db 全部缺失，改用已还原的源文件：{WORK_DIR}")
        for xlsx in sorted(WORK_DIR.glob("*.xlsx")):
            if xlsx.stat().st_size == 0:
                continue
            _, sheet = sheet_label(xlsx)
            print(f"  [就绪] {xlsx.name}")
            rebuilt.append((xlsx, sheet))
        dbs = []
    else:
        if missing:
            print("[WARN] 清单里的 db 不存在：", ", ".join(missing))
        dbs = [d for d in dbs if d.is_file()]
        print(f"待恢复 db：{len(dbs)} 个（取自固定清单，不扫目录）\n")

    WORK_DIR.mkdir(parents=True, exist_ok=True)

    for db in dbs:
        if db.stat().st_size == 0:
            print(f"  [跳过] {db.name}：空文件")
            continue
        _, sheet = sheet_label(db)
        # 同一数据集的多个 sheet 合并进一个 xlsx（引擎按 sheet 名存储）
        ds_id, _ = sheet_label(db)
        safe = re.sub(r'[\\/:*?"<>|]', "_", sheet)
        out = WORK_DIR / f"{ds_id}_{safe}.xlsx"
        # 已还原过就直接复用：从 db 反写 54 万行要几分钟，不能每次重跑
        if out.is_file() and out.stat().st_size > 0:
            print(f"  [复用] {db.name} → {out.name}（已存在，跳过还原）", flush=True)
            rebuilt.append((out, sheet))
            continue
        try:
            info = dump_db(db, out)
        except Exception as error:  # noqa: BLE001
            print(f"  [失败] {db.name}：{error}")
            continue
        if info.get("skipped"):
            print(f"  [跳过] {db.name}：{info['skipped']}")
            continue
        print(f"  [还原] {db.name} → {out.name}（{info['rows']:,} 行 / {info['cols']} 列）",
              flush=True)
        rebuilt.append((out, sheet))

    if not rebuilt:
        print("\n没有可还原的数据。")
        return 1

    print(f"\n共还原 {len(rebuilt)} 个工作表。")
    if args.dry_run:
        print("dry-run：未写入索引。去掉 --dry-run 执行实际导入。")
        return 0

    # 一个还原文件 = 一个数据集（每个文件里只有一个 Sheet1），
    # 逐个入库即可；文件名解析走 sheet_label，所以显示名带上原 sheet 名。
    ok, bad = 0, []
    for path, sheet in rebuilt:
        # 显示名必须能区分来源：多个 db 的 sheet 都叫 Sheet1、内容大小也相同，
        # 用同一个显示名会被引擎的「同名同大小」去重拦掉。
        ds_id, _ = sheet_label(path)
        display = f"恢复_{ds_id}_{sheet}.xlsx"
        # 已经用这个显示名入过库就跳过，让脚本可以反复跑而不产生重复记录
        if any(d.get("name") == display for d in engine.STATE.get("datasets") or []):
            print(f"  [跳过] {display}：已在索引中", flush=True)
            continue
        try:
            ds = engine.import_file(str(path), copy=True,
                                    display_name=display)
            ds["note"] = "从 db 缓存恢复"
            rows = sum(s.get("rows", 0) for s in ds.get("sheets") or [])
            ok += 1
            print(f"  [入库] {display} → {ds['id']}"
                  f"（{len(ds.get('sheets') or [])} 个工作表 / {rows:,} 行）",
                  flush=True)
        except Exception as error:  # noqa: BLE001
            bad.append(f"{display}: {error}")
            print(f"  [失败] {display}：{error}", flush=True)

    engine.save_state()
    print(f"\n入库成功 {ok} 个，失败 {len(bad)} 个。")
    print(f"索引已写回：{engine._index_file()}")
    for item in bad:
        print("  -", item)
    return 0 if ok and not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())