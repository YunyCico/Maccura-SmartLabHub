# -*- coding: utf-8 -*-
"""迁移：把大表数据库从「中文表名转下划线」的旧命名改成「带哈希」的唯一命名。

★ 为什么要迁：旧命名把不同中文表名压成同一个文件名
  （生化MAX / 免疫MAX → __MAX.db），后导入的会 os.remove 掉前一个的库文件，
  于是「生化MAX」查出来是「免疫MAX」的数据 —— 数据串表。

规则：
  · 没撞过车的 → 直接改文件名，索引里的 dbfile 同步更新（秒级，不丢数据）
  · 撞过车的   → 库文件内容已经被覆盖，**只能整个数据集重新导入**（源文件在 sources/ 里）
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(r"C:\Users\98319\Desktop\工作平台\SmartLabHub")
sys.path.insert(0, str(ROOT))
from modules.data_analysis import smartlab_engine as engine  # noqa: E402


def legacy_rel(ds_id: str, sheet_name: str) -> str:
    return os.path.join("db", "%s_%s.db" % (ds_id, re.sub(r"[^A-Za-z0-9_-]", "_", sheet_name)))


def clean_display_name(name: str) -> str:
    """去掉 sources 副本文件名里的内部前缀（ds_<10位hex>_）。

    不去掉的话，界面上的数据集名会变成
    「ds_8818920774_2026年7月1日...xlsx」这种内部名，用户根本认不出来。
    """
    previous = None
    while previous != name:
        previous = name
        name = re.sub(r"^ds_[0-9a-f]{10}_", "", name)
    return name


def detect_collisions(dataset) -> list[list[str]]:
    groups: dict[str, list[str]] = {}
    for sheet in dataset.get("sheets") or []:
        name = sheet.get("name") or ""
        groups.setdefault(legacy_rel(dataset["id"], name), []).append(name)
    return [names for names in groups.values() if len(names) > 1]


def main() -> int:
    engine.load_state()
    datasets = list(engine.STATE.get("datasets") or [])
    changed = 0
    for dataset in list(datasets):
        ds_id = dataset["id"]
        collisions = detect_collisions(dataset)
        if collisions:
            print("\n[%s] 撞车的表：%s" % (dataset["name"], " | ".join("/".join(g) for g in collisions)))
            print("   → 库文件内容已被互相覆盖，**整个数据集重新导入**")
            # 1) 先删掉旧的库文件
            db_dir = Path(engine.LIB_DIR) / "db"
            for sheet in dataset.get("sheets") or []:
                if not sheet.get("dbfile"):
                    continue
                old = db_dir / Path(sheet["dbfile"]).name
                if old.is_file():
                    old.unlink()
            # 2) 从索引里摘掉这条记录（源文件副本还在 sources/ 里）
            source = Path(engine.ds_abs_path(dataset))
            engine.STATE["datasets"] = [d for d in engine.STATE["datasets"] if d["id"] != ds_id]
            engine.save_state()
            # 3) 用修复后的引擎重新导入
            #    ★ import_file 一定会复制一份副本（它忽略 copy 参数），
            #      所以重新导入后要把旧副本删掉，别在 sources 里堆重复文件。
            started = time.time()
            fresh = engine.import_file(str(source), copy=True, display_name=dataset["name"])
            new_source = Path(engine.ds_abs_path(fresh))
            if new_source.resolve() != source.resolve() and source.is_file():
                source.unlink()
            # ★ 必须落盘！漏了这一句，新数据集只活在内存里，索引等于没加。
            engine.save_state()
            print("   → 重新导入完成，%.1f 秒（%s）" % (time.time() - started, fresh["id"]))
            for sheet in fresh.get("sheets") or []:
                if sheet.get("dbfile"):
                    print("      %-24s → %s" % (sheet["name"], sheet["dbfile"]))
            changed += 1
            continue

        # 没撞车：直接改名
        renamed = 0
        for sheet in dataset.get("sheets") or []:
            if not sheet.get("dbfile"):
                continue
            name = sheet.get("name") or ""
            old_rel = sheet["dbfile"]
            new_rel = engine._sheet_db_rel(ds_id, name).replace("\\", "/")
            if os.path.normpath(old_rel) == os.path.normpath(new_rel):
                continue
            old_abs = os.path.join(engine.LIB_DIR, old_rel)
            new_abs = os.path.join(engine.LIB_DIR, new_rel)
            if not os.path.isfile(old_abs):
                print("   ⚠️ 旧库不存在，跳过：%s" % old_rel)
                continue
            if os.path.exists(new_abs):
                print("   ⚠️ 目标已存在，跳过：%s" % new_rel)
                continue
            os.replace(old_abs, new_abs)
            sheet["dbfile"] = new_rel
            renamed += 1
        if renamed:
            engine.save_state()
            print("\n[%s] 改名 %d 个库文件" % (dataset["name"], renamed))
            changed += 1

    # ---- 补回「sources 里有副本、但索引里没有」的数据集 ----
    # （迁移过程中可能已经把记录从索引里摘掉、但没来得及写回）
    referenced_sources = set()
    for dataset in engine.STATE.get("datasets") or []:
        referenced_sources.add(os.path.basename(engine.ds_abs_path(dataset)))
    src_dir = Path(engine.SRC_DIR)
    if src_dir.is_dir():
        for path in sorted(src_dir.glob("*.xlsx")):
            if path.name in referenced_sources:
                continue
            print("\n[sources 里的孤儿副本] 重新导入：%s" % path.name)
            started = time.time()
            fresh = engine.import_file(str(path), copy=True,
                                       display_name=clean_display_name(path.name))
            new_source = Path(engine.ds_abs_path(fresh))
            if new_source.resolve() != path.resolve() and path.is_file():
                path.unlink()
            engine.save_state()
            print("   → 重新导入完成，%.1f 秒（%s）" % (time.time() - started, fresh["id"]))
            changed += 1

    # 清理没被任何数据集引用的孤儿库
    referenced = set()
    for dataset in engine.STATE.get("datasets") or []:
        for sheet in dataset.get("sheets") or []:
            if sheet.get("dbfile"):
                referenced.add(os.path.basename(sheet["dbfile"]))
    orphans = []
    db_dir = Path(engine.LIB_DIR) / "db"
    if db_dir.is_dir():
        for path in db_dir.iterdir():
            if path.is_file() and path.name not in referenced:
                orphans.append(path)
                path.unlink()
    if orphans:
        print("\n清理孤儿库文件 %d 个" % len(orphans))

    # 复核：每个库文件的行数必须与索引记录一致
    print("\n=== 复核：库文件行数 vs 索引行数 ===")
    bad = 0
    for dataset in engine.STATE.get("datasets") or []:
        for sheet in dataset.get("sheets") or []:
            if not sheet.get("dbfile"):
                continue
            path = os.path.join(engine.LIB_DIR, sheet["dbfile"])
            if not os.path.isfile(path):
                print("  ❌ 缺失 %s / %s" % (dataset["name"], sheet["name"]))
                bad += 1
                continue
            import sqlite3
            con = sqlite3.connect(path)
            real = con.execute("SELECT COUNT(*) FROM data").fetchone()[0]
            con.close()
            expect = int(sheet.get("rows") or 0) + int(sheet.get("skip_rows") or 0)
            if real != expect:
                print("  ❌ %s / %s : 索引 %d 行，库 %d 行" % (dataset["name"], sheet["name"], expect, real))
                bad += 1
    print("  全部一致 ✅" if bad == 0 else "  仍有 %d 处不一致 ❌" % bad)
    print("\n改动的数据集：%d 个" % changed)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
