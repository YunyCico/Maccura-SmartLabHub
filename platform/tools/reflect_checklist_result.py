# -*- coding: utf-8 -*-
"""把用户核实的结果回写到《LIS未命中项目核对表.xlsx》。

用户只在 D 列（品名）手写了 7 个品名，F/G/H/I（专业/组套/处理方式/备注）留空。
本脚本按已落地的分类表把这 7 项的专业/组套回填，并在 I 列写明处理方式，
其余 74 项保持原样（留空不分类）—— 用户已明确「其他的不管了」。

用法：python platform/tools/reflect_checklist_result.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill

PROJECT_ROOT = Path(__file__).resolve().parents[2]
for _p in (PROJECT_ROOT, PROJECT_ROOT / "modules", PROJECT_ROOT / "platform" / "backend"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from app.services import lis_service as L  # noqa: E402

CHECKLIST = Path(r"C:\Users\98319\Desktop\LIS未命中项目核对表.xlsx")
NOTE_HEADER = "备注/你的说明"

# 用户在 D 列手填的品名 → 处理说明
NOTES = {
    "钾": "已进分类表：生化/电解质类（品名「钾」，简称 K）",
    "钠": "已进分类表：生化/电解质类（品名「钠」，简称 Na）",
    "氯": "已进分类表：生化/电解质类（品名「氯」，简称 Cl）",
    "白带": "已进分类表：临检/体液类（品名「白带」，组套为新立，待你确认）",
    "血气": "已进分类表：生化/血气类（品名「血气」，简称 ABG，组套为新立，待你确认）",
    "白介素6": "分类表已有「白介素-6（直接化学发光法）」/ IL-6 → 免疫/炎症类，只做名称翻译，不加行",
    "便隐血": "已进分类表：临检/体液类（品名「便隐血」，简称 FOB，组套为新立，待你确认）",
}

GREEN = PatternFill("solid", fgColor="E3F4E4")
GREY = PatternFill("solid", fgColor="F2F2F2")


def main() -> int:
    if not CHECKLIST.is_file():
        print(f"核对表不存在：{CHECKLIST}")
        return 1

    wb = openpyxl.load_workbook(CHECKLIST)
    ws = wb["未命中项目核对表"]

    headers = [str(c.value or "") for c in ws[1]]
    col_d = headers.index("★高置信参考（剥后缀/组套名/英文缩写精确命中，可直接采纳）") + 1
    col_f = headers.index("专业（填）") + 1
    col_g = headers.index("组套（填）") + 1
    col_h = headers.index("处理方式（填）") + 1
    col_i = headers.index(NOTE_HEADER) + 1

    done = 0
    for row in range(2, ws.max_row + 1):
        name = str(ws.cell(row=row, column=2).value or "")
        filled = str(ws.cell(row=row, column=col_d).value or "").strip()
        if not filled:
            # 用户没填的行：保持留空不分类，标灰表示"不管了"
            for col in (col_f, col_g, col_h):
                ws.cell(row=row, column=col).fill = GREY
            continue
        target = NOTES.get(filled)
        if not target:
            continue
        result = L.classify_project(name, "")
        ws.cell(row=row, column=col_f).value = result["test_specialty"]
        ws.cell(row=row, column=col_g).value = result["test_suite"]
        ws.cell(row=row, column=col_h).value = "进分类表"
        ws.cell(row=row, column=col_i).value = target
        for col in (col_f, col_g, col_h, col_i):
            ws.cell(row=row, column=col).fill = GREEN
        done += 1
        print(f"  {name} → {result['test_specialty']}/{result['test_suite']}")

    # 新增一列：当前系统的分类结果（以代码为准）
    col_j = ws.max_column + 1
    head = ws.cell(row=1, column=col_j)
    head.value = "当前系统判定（2026-10-20）"
    head.font = Font(bold=True, color="FFFFFF", size=11)
    head.fill = PatternFill("solid", fgColor="1B5FAA")
    head.alignment = Alignment(horizontal="center", vertical="center")
    ws.column_dimensions["J"].width = 30
    for row in range(2, ws.max_row + 1):
        name = str(ws.cell(row=row, column=2).value or "")
        result = L.classify_project(name, "")
        cell = ws.cell(row=row, column=col_j)
        cell.value = f"{result['test_specialty']}/{result['test_suite']}" if result["test_specialty"] else "未命中"
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        if result["test_specialty"]:
            cell.fill = GREEN

    wb.save(CHECKLIST)
    print(f"已回填 {done} 项，其余 {ws.max_row - 1 - done} 项保持留空 → {CHECKLIST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
