# -*- coding: utf-8 -*-
"""把用户核实的 LIS 未命中项目落进试剂分类表。

背景
----
2026-10-20，用户在《LIS未命中项目核对表.xlsx》的 D 列（品名）手写了 7 个品名：

    血清钾测定      → 钾
    血清钠测定      → 钠
    血清氯测定      → 氯
    白带检查        → 白带
    血气分析        → 血气
    白细胞介素6    → 白介素6
    粪便隐血        → 便隐血

口径
----
· 品名在分类表里不存在 → 往「组套核对」表**加新行**（别名映射造不出分类）。
· 品名在分类表里已存在（白介素6 ≈ 白介素-6/IL-6）→ 只做名称翻译，不重复加行。
· 专业 / 组套：优先用分类表现有组套；确需立新组套的，写进「说明」表的待确认清单。
· 绝不替换 LIS 原始项目名 —— 本脚本只动分类表，不碰任何数据行。

用法
----
    python platform/tools/apply_taxonomy_additions.py            # 写入
    python platform/tools/apply_taxonomy_additions.py --dry-run  # 只看要加什么
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import openpyxl

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TAXONOMY = PROJECT_ROOT / "platform" / "backend" / "app" / "resources" / "LIS项目分类表.xlsx"
BACKUP_DIR = PROJECT_ROOT / "platform" / "tools" / "_backup"

# (专业, 组套, 品名, 产品简称)  —— 顺序即写入顺序
ADDITIONS = [
    ("生化", "电解质类", "钾", "K"),
    ("生化", "电解质类", "钠", "Na"),
    ("生化", "电解质类", "氯", "Cl"),
    ("生化", "血气类", "血气", "ABG"),
    ("临检", "体液类", "白带", ""),
    ("临检", "体液类", "便隐血", "FOB"),
]

# 已经在分类表里、不需要加行，只需做名称翻译的（分类仍回分类表查，不硬编码）
# 「白细胞介素6」→ 分类表里的「白介素-6（直接化学发光法）」/ IL-6（免疫/炎症类）
# 「粪便隐血」→ 新增品名「便隐血」（临检/体液类）
NAME_ONLY = [
    ("白细胞介素6", "白介素6"),
    ("粪便隐血", "便隐血"),
]

# 新立组套要登记到「说明」表，避免以后有人以为是原始总表里就有的
NEW_SUITE_NOTES = [
    ("血气类", "2 项｜生化专业血气分析（动脉血气）—— 总表未给 5级分类，按专业惯例立组"),
    ("体液类", "2 项｜临检体液标本（白带、粪便隐血）—— 总表未给 5级分类，按专业惯例立组"),
]


def _norm(text: object) -> str:
    return str(text or "").strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not TAXONOMY.is_file():
        print(f"分类表不存在：{TAXONOMY}")
        return 1

    wb = openpyxl.load_workbook(TAXONOMY)
    ws = wb["组套核对"]
    existing = set()
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row and row[2]:
            existing.add(_norm(row[2]))

    todo = [item for item in ADDITIONS if item[2] not in existing]
    skipped = [item for item in ADDITIONS if item[2] in existing]
    print(f"分类表现有品名 {len(existing)} 个；本次需新增 {len(todo)} 行，已存在跳过 {len(skipped)} 行")
    for item in todo:
        print("  + ", " | ".join(item))
    for item in skipped:
        print("  = 已存在", " | ".join(item))
    for cn, target in NAME_ONLY:
        print(f"  ~ 名称翻译 {cn} → {target}（分类表已有该品名，不加行）")

    if args.dry_run:
        return 0
    if not todo:
        print("无需写入，分类表已是最新。")
        return 0

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup = BACKUP_DIR / "LIS项目分类表_before_additions.xlsx"
    shutil.copy2(TAXONOMY, backup)
    print(f"已备份 → {backup}")

    for specialty, suite, name, abbr in todo:
        ws.append([specialty, suite, name, abbr])
        print(f"  写入：{specialty} | {suite} | {name} | {abbr}")

    if "说明" in wb.sheetnames:
        note = wb["说明"]
        for suite, text in NEW_SUITE_NOTES:
            if not any(_norm(r[0]) == suite for r in note.iter_rows(min_row=2, values_only=True) if r):
                note.append([suite, text])
                print(f"  说明登记：{suite}")

    wb.save(TAXONOMY)
    print(f"已保存 → {TAXONOMY}（组套核对 {ws.max_row} 行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
