# -*- coding: utf-8 -*-
"""对项目分类表按「品名」去重 —— 同一个品名只保留一条。

用在TAT 分析的匹配词典上：同一个项目在原表里会因仪器平台不同出现多次
（糖化血红蛋白出现 13 次），但匹配时只需要知道"它属于哪个专业/组套"，
重复条目没有意义，反而会让匹配表变大、拖慢匹配。

去重规则（按优先级）：
    1. 组套非空的行优先保留（组套是你要用来分组的，空着没用）
    2. 组套仍为空或仍有多个不同值 → 取组套字符串排序后的第一个非空
    3. 产品简称取首个非空值
    4. 匹配键 = 品名主干 + 简称（同一条目内合并去重）
    5. 若一个品名下的多个条目**专业不同**（跨专业复用同一品名），
       保留首个出现的专业，但在报告里列出，交由人工确认

用法：
    python platform/tools/dedupe_taxonomy.py --dry-run
    python platform/tools/dedupe_taxonomy.py -o 项目分类表_去重.xlsx
"""
from __future__ import annotations

import argparse
import re
import unicodedata
from collections import Counter, OrderedDict
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

SRC_DEFAULT = Path(r"C:\Users\98319\Desktop\试剂分类.xlsx")

HEADERS = ["专业", "组套", "品名", "产品简称", "匹配键"]
WIDTHS = [10, 22, 54, 16, 60]

HEAD_FILL = PatternFill("solid", fgColor="1F4E79")
HEAD_FONT = Font(color="FFFFFF", bold=True, size=11)

# Excel 里表示"没填"的占位值，不能当简称/组套用
JUNK = {"#N/A", "N/A", "NA", "na", "-", "无", "null", "None", "#VALUE!", "#NAME?"}


def _s(v) -> str:
    s = str(v).strip() if v is not None else ""
    return "" if s in JUNK else s


def norm_key(s: str) -> str:
    """把"看着一样、实际字符不同"的品名归一到同一个键。

    原表这类假重复很多，肉眼完全分不出：
        5’-核苷酸酶 撇号是U+2019（右单引号）
        5＇-核苷酸酶 撇号是 U+FF07（全角撇号）
        前白蛋白(免疫比浊法)  括号 U+0028/0029（半角）
        前白蛋白（免疫比浊法）括号 U+FF08/FF09（全角）

    做法：
        NFKC       全角→半角（连带 U+FF07 → U+2019）
        引号统一   ’‘ → '
        去掉空白   LIS 导出的项目名常带多余空格
    归一后仍保留原始串用于展示，只把归一结果当去重键。
    """
    s = unicodedata.normalize("NFKC", s or "")
    s = s.replace("\u2019", "'").replace("\u2018", "'")
    s = re.sub(r"\s+", "", s)
    return s


def dedupe(src: Path):
    wb = openpyxl.load_workbook(src, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(min_row=2, values_only=True))

    # 保持首次出现顺序。**分组键用归一后的品名**，否则全角/半角括号、
    # 直/弯撇号差异会漏过去重（肉眼看完全一样）。
    buckets: OrderedDict[str, list] = OrderedDict()
    for r in rows:
        name = _s(r[2]) if len(r) > 2 else ""
        if not name:
            continue
        key = norm_key(name)
        buckets.setdefault(key, []).append({
            "专业": _s(r[0]) if len(r) > 0 else "",
            "组套": _s(r[1]) if len(r) > 1 else "",
            "产品简称": _s(r[3]) if len(r) > 3 else "",
            "匹配键": _s(r[4]) if len(r) > 4 else "",
            "_品名原样": name,
        })

    out, conflicts = [], []
    for key_norm, items in buckets.items():
        # ① 组套非空的优先
        with_group = [x for x in items if x["组套"]]
        pool = with_group or items

        # ② 组套取排序后的第一个非空（保证同一品名结果稳定）
        groups = sorted({x["组套"] for x in pool if x["组套"]})
        group = groups[0] if groups else ""

        # ③ 简称取首个非空
        short = next((x["产品简称"] for x in pool if x["产品简称"]), "")

        # ④ 专业取首个非空
        prof = next((x["专业"] for x in pool if x["专业"]), "")

        # ⑤ 展示用品名：取最常见的原始写法（多写法时保留最主流的那个）
        variants = Counter(x["_品名原样"] for x in pool)
        disp_name = variants.most_common(1)[0][0]

        # ⑥ 匹配键 = 归一后的品名主干 + 全部简称变体
        #    同一项目常有两种写法（Crea / Crea Gen.2、RF / RF Gen.2），
        #    LIS 导出的"检测项目"两种都可能写，所以变体全收，匹配时才不会漏。
        keys = []
        for k in (key_norm,):
            if k and k not in keys:
                keys.append(k)
        for x in pool:
            for part in (x["匹配键"] or "").split("|"):
                p = norm_key(part)
                if p and p not in keys:
                    keys.append(p)
        key = " | ".join(keys)

        out.append({
            "专业": prof, "组套": group, "品名": disp_name,
            "产品简称": short, "匹配键": key,
            "_次数": len(items),
        })

        # 记录需要人工看的分歧
        profs = {x["专业"] for x in items if x["专业"]}
        gset = {x["组套"] for x in items if x["组套"]}
        sgs = {x["产品简称"] for x in items if x["产品简称"]}
        nameset = {x["_品名原样"] for x in items}
        if len(profs) > 1 or len(gset) > 1 or len(sgs) > 1 or len(nameset) > 1:
            conflicts.append((disp_name, sorted(profs), sorted(gset), sorted(sgs),
                              len(items), sorted(nameset)))
    return out, conflicts, len(rows)


def report(out, conflicts, total_in):
    print("=" * 64)
    print(f"去重结果：{total_in} 行 → {len(out)} 行（去掉 {total_in - len(out)} 行重复）")
    print("=" * 64)
    print("\n【专业分布】")
    for k, n in Counter(x["专业"] for x in out).most_common():
        gs = len({x["组套"] for x in out if x["专业"] == k and x["组套"]})
        print(f"   {k:10s} {n:4d} 条   下辖 {gs} 个组套")
    print("\n【组套数量】", len({x["组套"] for x in out if x["组套"]}))
    print("\n【去重后 Top10 重复项（原出现次数）】")
    for x in sorted(out, key=lambda x: -x["_次数"])[:10]:
        print(f"   {x['_次数']:3d} 次  {x['品名'][:44]:46s} {x['组套'] or '（组套空）'}")
    if conflicts:
        print(f"\n【需人工确认】{len(conflicts)} 个品名存在属性分歧（已按规则择一保留）")
        for name, ps, gs, ss, n, nameset in conflicts[:12]:
            print(f"   {name[:38]:40s} ×{n}  专业{ps} 组套{gs} 简称{ss}")
            if len(nameset) > 1:
                print(f"        写法差异：{' / '.join(v[:26] for v in nameset[:3])}")


def write_xlsx(out, path: Path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "项目分类表"
    ws.append(HEADERS)
    for c in ws[1]:
        c.fill, c.font = HEAD_FILL, HEAD_FONT
        c.alignment = Alignment(horizontal="center", vertical="center")
    ws.freeze_panes = "A2"

    order = {"生化": 0, "免疫": 1, "临检": 2, "分子": 3, "数字PCR": 4, "": 8}
    for r in sorted(out, key=lambda x: (order.get(x["专业"], 5), x["组套"], x["品名"])):
        ws.append([r[h] for h in HEADERS])

    for i, w in enumerate(WIDTHS, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(vertical="center")
    ws.auto_filter.ref = f"A1:{get_column_letter(len(HEADERS))}{ws.max_row}"

    # 统计页
    s2 = wb.create_sheet("统计")
    s2.append(["层级", "名称", "数量"])
    for c in s2[1]:
        c.fill, c.font = HEAD_FILL, HEAD_FONT
    for p, n in Counter(x["专业"] for x in out).most_common():
        s2.append(["第一层·专业", p, n])
    for g, n in Counter(x["组套"] for x in out if x["组套"]).most_common():
        s2.append(["第二层·组套", g, n])
    s2.column_dimensions["A"].width = 16
    s2.column_dimensions["B"].width = 28
    s2.column_dimensions["C"].width = 10

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(path))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", nargs="?", default=str(SRC_DEFAULT))
    ap.add_argument("-o", "--out", default="项目分类表_去重.xlsx")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    out, conflicts, total_in = dedupe(Path(a.src))
    report(out, conflicts, total_in)
    if a.dry_run:
        print("\ndry-run：未写文件")
        return
    p = Path(a.out)
    write_xlsx(out, p)
    print(f"\n已生成：{p.resolve()}")


if __name__ == "__main__":
    main()