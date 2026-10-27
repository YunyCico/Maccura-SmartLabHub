r"""生成 LIS 未命中项目核对表（带下拉 + 填表说明）。

用法：python platform/tools/build_miss_checklist.py
输出：C:\Users\98319\Desktop\LIS未命中项目核对表.xlsx
"""
from __future__ import annotations

import collections
import re
import os
import sys
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.datavalidation import DataValidation

PROJECT_ROOT = Path(__file__).resolve().parents[2]
for _p in (PROJECT_ROOT, PROJECT_ROOT / "modules", PROJECT_ROOT / "platform" / "backend"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from app.services import lis_service as L  # noqa: E402

BASE = Path(r"C:\Users\98319\Desktop\LIS数据表单")
SRC = BASE / "爱成" / "2026年7月1日至8月13日标本明细-生化临检免疫(1).xlsx"
OUT = Path(r"C:\Users\98319\Desktop\LIS未命中项目核对表.xlsx")

ACTIONS = "进分类表,仅加别名映射,留空不分类,删除该送检名"


def collect_projects() -> collections.Counter:
    counter: collections.Counter = collections.Counter()
    wb = openpyxl.load_workbook(SRC, read_only=True, data_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        for row in ws.iter_rows(min_row=2, values_only=True):
            for item in str(row[15] or "").split(","):
                item = item.strip()
                if item:
                    counter[item] += 1
    finally:
        wb.close()
    return counter


def _strip_digits(key: str) -> str:
    """去掉末尾的数字（肝功能2 → 肝功能、OGTT0 → OGTT）。"""
    return key.rstrip("0123456789") or key


def _strip_count_suffix(key: str) -> str:
    """去掉括号里的项数（电解质（七项） → 电解质、OGTT(1小时) → OGTT）。"""
    return re.sub(r"[（(][^（()）]*[)）]\s*$", "", key).strip() or key


def build_candidates(cn: str, nameidx, enidx, suiteidx) -> tuple[str, str]:
    """返回 (高置信参考, 模糊提示)。

    高置信 = 剥修饰词后前缀精确命中 / 组套名命中 / 括号内英文缩写命中简称。
    模糊提示 = 关键词字面相近，仅供"分类表里其实有没有类似的东西"参考，不保证对。
    """
    key = L._norm_key(cn)
    strong: list[str] = []
    weak: list[str] = []

    # 1) 剥修饰词后做前缀匹配
    stripped = L._strip_name_noise(cn)
    for label, probe in (("剥后缀", stripped), ("原名", key)):
        if not probe or len(probe) < 2:
            continue
        for index, tag in ((nameidx, "品名"), (enidx, "简称")):
            matched = [i for i in index if len(i) >= len(probe) and i.startswith(probe)]
            if matched:
                best = min(matched, key=len)
                entry = index[best]
                strong.append(f"{tag}前缀[{best}]→{entry['specialty']}/{entry['suite']}")
                break
        if strong:
            break

    # 2) 组套级别：剥数字/项数后再比对组套别名
    if not strong:
        for probe in (key, _strip_count_suffix(key), _strip_digits(_strip_count_suffix(key))):
            if not probe or len(probe) < 2:
                continue
            for alias, suite in L._SUITE_ALIASES.items():
                if L._norm_key(alias) != probe:
                    continue
                suite_key = L._norm_key(suite)
                if suite_key in suiteidx:
                    entry = suiteidx[suite_key]
                    strong.append(f"组套名[{alias}→{suite}]→{entry['specialty']}/{entry['suite']}")
                else:
                    strong.append(f"组套名[{alias}→{suite}]（分类表暂无此组套）")
                break
            if strong:
                break

    # 3) 括号里的英文缩写精确命中产品简称
    if not strong:
        for segment in L._inner_segments(cn):
            if segment in enidx:
                entry = enidx[segment]
                strong.append(f"括号内缩写[{segment}]→{entry['specialty']}/{entry['suite']}")
                break

    # 4) 模糊提示：只给同专业的相近品名，且要求关键词长度 >= 3
    if not strong:
        short = _strip_digits(_strip_count_suffix(key))
        for size in (4, 3):
            for start in range(len(short) - size + 1):
                piece = short[start:start + size]
                matched = [i for i in nameidx if piece in i]
                if matched:
                    best = max(matched, key=len)
                    entry = nameidx[best]
                    weak.append(f"含「{piece}」→品名[{best}]→{entry['specialty']}/{entry['suite']}")
                    break
            if weak:
                break

    strong_text = " ｜ ".join(strong[:2]) if strong else ""
    weak_text = " ｜ ".join(weak[:2]) if weak else ""
    return strong_text, weak_text


def main() -> int:
    counter = collect_projects()
    misses = [
        (name, count)
        for name, count in counter.most_common()
        if not L.classify_project(name, "")["test_specialty"]
    ]

    taxonomy = L._load_taxonomy()
    nameidx = taxonomy["name"]
    enidx = taxonomy["en"]
    suiteidx = taxonomy["suite"]
    specs = sorted({v["specialty"] for v in nameidx.values()})
    suites = sorted({v["suite"] for v in nameidx.values()})

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "未命中项目核对表"
    ws.append([
        "序号", "LIS项目名（原样）", "出现次数",
        "★高置信参考（剥后缀/组套名/英文缩写精确命中，可直接采纳）",
        "模糊提示（仅供判断，分类表里字面相近，不保证对）",
        "专业（填）", "组套（填）", "处理方式（填）", "备注/你的说明",
    ])

    for index, (name, count) in enumerate(misses, 1):
        strong, weak = build_candidates(name, nameidx, enidx, suiteidx)
        ws.append([
            index, name, count, strong, weak, "", "", "", "",
        ])

    head_fill = PatternFill("solid", fgColor="1B5FAA")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF", size=11)
        cell.fill = head_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.freeze_panes = "A2"
    for column, width in zip("ABCDEFGHI", [6, 26, 10, 44, 44, 14, 18, 20, 34]):
        ws.column_dimensions[column].width = width
    thin = Side(style="thin", color="D5E0ED")
    for row in ws.iter_rows(min_row=1, max_row=ws.max_row, max_col=9):
        for cell in row:
            cell.border = Border(left=thin, right=thin, top=thin, bottom=thin)
            cell.alignment = Alignment(vertical="center", wrap_text=True)

    dv_spec = DataValidation(type="list", formula1='"' + ",".join(specs) + '"', allow_blank=True)
    dv_suite = DataValidation(type="list", formula1='"' + ",".join(suites) + '"', allow_blank=True)
    dv_action = DataValidation(type="list", formula1='"' + ACTIONS + '"', allow_blank=True)
    for dv, column in ((dv_spec, "F"), (dv_suite, "G"), (dv_action, "H")):
        ws.add_data_validation(dv)
        dv.add(f"{column}2:{column}500")

    # 填表说明
    guide = wb.create_sheet("怎么填")
    guide.column_dimensions["A"].width = 24
    guide.column_dimensions["B"].width = 100
    guide.append(["LIS 未命中项目 · 填表说明", ""])
    guide["A1"].font = Font(bold=True, size=14)
    guide_rows = [
        ("这份表是什么",
         "爱成「2026年7月1日至8月13日标本明细-生化临检免疫」里的「检验目的」列，"
         "共 201 个唯一项目，其中 81 项在试剂分类表里查不到专业/组套。"),
        ("D 列 分类表候选参考",
         "系统自动查分类表的结果，告诉你这个项目最接近哪一行。仅供参考，可以不听。"),
        ("E 列 专业（填）",
         "下拉可选：" + "、".join(specs) + "。如果分类表里没有你要的专业，写在 H 列告诉我。"),
        ("F 列 组套（填）",
         "下拉共 41 个组套（肝功类、肾功类、电解质类…），选和 E 列配套的那一个。"),
        ("G 列 处理方式（填）", "四选一：\n"
         "· 进分类表 —— 分类表里确实缺这个试剂，你确认后我作为新行加进 LIS项目分类表.xlsx\n"
         "· 仅加别名映射 —— 分类表里有对应试剂，只是名字写法不同（我加一条名称对照即可，不动分类表）\n"
         "· 留空不分类 —— 这个项目确实归不了类，导出时专业/组套留空\n"
         "· 删除该送检名 —— 这个送检名本身不该出现，建议从 LIS 送检项目里去掉"),
        ("I 列 备注/你的说明",
         "任何补充，比如「这个其实是 XX 检测」「归到自定义组套」「这个名有错别字应改成 XX」。"),
        ("两种处理方式的区别",
         "「仅加别名映射」= 不动分类表，代码里加一条中文名→英文缩写的对照。\n"
         "「进分类表」= 真的往 LIS项目分类表.xlsx 里加新行（品名 + 产品简称 + 专业 + 组套），"
         "以后别的实验室遇到同类项目也能自动命中。\n"
         "拿不准就选「进分类表」，我按你说的做。"),
        ("填完给我",
         "把这个 xlsx 发我，或把 F/G/H/I 四列的内容贴给我也行。我按你填的改，"
         "改完跑一遍全量命中率给你看。"),
    ]
    for left, right in guide_rows:
        guide.append([left, right])
    for row in range(2, guide.max_row + 1):
        guide.cell(row=row, column=1).font = Font(bold=True)
        guide.cell(row=row, column=1).alignment = Alignment(vertical="top")
        guide.cell(row=row, column=2).alignment = Alignment(vertical="top", wrap_text=True)

    # 分类表现有组套
    suite_sheet = wb.create_sheet("分类表现有组套")
    suite_sheet.append(["专业", "组套", "该组套下试剂条数"])
    counts: collections.Counter = collections.Counter()
    for value in nameidx.values():
        counts[(value["specialty"], value["suite"])] += 1
    for (spec, suite), count in sorted(counts.items()):
        suite_sheet.append([spec, suite, count])
    for cell in suite_sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = head_fill
    for column, width in zip("ABC", [12, 26, 18]):
        suite_sheet.column_dimensions[column].width = width

    wb.save(OUT)
    print("saved:", OUT)
    print("未命中行数:", len(misses))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
