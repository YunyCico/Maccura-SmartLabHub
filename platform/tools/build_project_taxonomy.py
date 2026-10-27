# -*- coding: utf-8 -*-
"""把《试剂分类.xlsx》整理成 TAT 分析要用的三层分类表。

三层结构（梦云 2026-10-19 定）：
    第一层 专业：生化 / 免疫 / 临检 / 分子 / 数字PCR
    第二层 组套：肝功类 / 肾功类 / 肿瘤类 …（原「5级分类」改名）
    第三层 单项：具体检测项目（品名 + 产品简称，用于匹配 LIS 的「检测项目」列）

原始表只有「5级分类 / 大类 / 品名 / 产品简称」四列，本脚本补出：
    · 专业层 —— 从大类前缀归纳（生化-C800 / 生化-LST / 免疫-i820 → 生化 / 免疫）
    · 组套层 —— 原 5级分类；为空的按「品名主干 + 简称」跨行回填（唯一命中才填）
    · 项目性质 —— 区分「检测项目」与「非检测项目」
      非检测项目 = 质控品 / 校准品 / 清洗液 / 染色液 / 溶血剂 / 装机试剂 / 耗材 / 仪器部件
      这些不会产生 TAT 样本，统计时要排除
    · 匹配键 —— 品名主干与简称，供系统侧做「检测项目 → 组套」反查

用法：
    python platform/tools/build_project_taxonomy.py --dry-run
    python platform/tools/build_project_taxonomy.py -o 项目分类表.xlsx
"""
from __future__ import annotations

import argparse
import re
from collections import Counter, defaultdict
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

SRC_DEFAULT = Path(r"C:\Users\98319\Desktop\试剂分类.xlsx")

# ---------------------------------------------------------------- 专业层
# 从「大类」前缀归纳专业。顺序敏感：先匹配长前缀，避免 '生化' 抢走 '生化-开放'。
PROF_RULES = [
    ("数字PCR", "数字PCR"),
    ("临检-", "临检"),
    ("临检", "临检"),
    ("免疫", "免疫"),
    ("生化-", "生化"),
    ("分子", "分子"),
    ("电解质项目", "生化"),
    ("糖化", "临检"),
    ("C800-", "生化"),
    ("25VD", "免疫"),
    ("SAA", "免疫"),
]
# 这些大类本身就是非检测用途
NONTEST_BIGS = {"校准包", "装机试剂", "装机耗材", "耗材"}

# 原表「大类」为空的行 —— 按品名关键词补专业（否则会掉进"未分类"）
# 注意：这些行多是质控/耗材（会再被项目性质筛掉），
# 但也有真检测项目（弓形虫/风疹 IgG、凝血因子、HPV、核酸提取），所以要分类。
PROF_HINT_KW = [
    ("凝血", "临检"), ("血细胞", "临检"), ("尿液", "临检"), ("血型", "临检"),
    ("抗人球蛋白", "临检"), ("交叉配血", "临检"), ("HBT", "临检"),
    ("核酸", "分子"), ("基因", "分子"), ("HPV", "分子"), ("弓形虫", "免疫"),
    ("风疹", "免疫"), ("巨细胞", "免疫"), ("单纯疱疹", "免疫"),
    ("发光剂", "免疫"), ("特定蛋白", "临检"),
]
# 明确非检测用途的大类 → 专业直接记为"非检测项目"
NONTEST_BIGS_PROF = {"校准包", "装机试剂", "装机耗材", "耗材"}

# ---------------------------------------------------------------- 组套层
# 原「5级分类」里这些值不是真正的组套，视为空
FAKE_GROUP = {"#N/A", "非成套件", "检测组件", "流体流路组件", "电子电气组件"}

# ---------------------------------------------------------------- 项目性质
NONTEST_KW = [
    "质控", "校准", "清洗", "清洁", "稀释", "装机", "耗材", "线性",
    "对照组", "检查液", "溶血剂", "染色液", "底物液", "贮存管", "稀释液",
    "液路油", "镜油", "鞘液", "聚焦液", "小钢珠", "吸嘴", "采样针",
    "保存液", "解冻液", "复染液", "固定液", "包埋液", "裂解液",
]
PART_KW = [
    "电极", "反应杯", "灯", "搅拌", "吸样针", "洗涤", "泵", "管路",
    "密封", "接头", "组件", "模块", "样本管帽", "载玻片", "前处理管",
]

# 大类本身就是一个独立检测领域，但原表没给组套 → 按大类直接立组套
BIG_AS_GROUP = {
    "临检-尿液": "尿液类",
    "临检-输血": "输血类",
    "临检-推片": "推片类",
    "临检-糖化": "糖化类",
    "临检-特定蛋白": "特殊蛋白类",
    "临检-血球": "血球类",
    "临检-凝血": "常规凝血类",
}


def _s(v) -> str:
    return str(v).strip() if v is not None else ""


def core_name(name: str) -> str:
    """品名主干：去掉括号里的方法说明与'试剂盒/试剂'字样，留可比对的项目名。"""
    out = name.split("（")[0].split("(")[0]
    for w in ("测定试剂盒", "试剂盒", "试剂"):
        out = out.replace(w, "")
    return out.strip()


def profession(big: str) -> str:
    if not big:
        return ""
    for k, v in PROF_RULES:
        if big.startswith(k):
            return v
    return "未分类"


def profession_guess(big: str, nm: str, sg: str) -> str:
    """大类为空时，按品名/简称关键词推断专业；推不出返回"未分类"。"""
    if big in NONTEST_BIGS_PROF:
        return "非检测项目"
    text = f"{nm} {sg}"
    for k, v in PROF_HINT_KW:
        if k in text:
            return v
    return "未分类"


def is_nontest(nm: str, sg: str, big: str) -> bool:
    if big in NONTEST_BIGS:
        return True
    text = f"{nm} {sg}"
    if any(k in text for k in NONTEST_KW):
        return True
    if any(k in text for k in PART_KW):
        return True
    return False


def build(src: Path):
    wb = openpyxl.load_workbook(src, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(min_row=2, values_only=True))

    # ---- 先建「品名主干/简称 → 组套」索引，供空组套回填 ----
    # global_pool：该项目在全表出现过的组套（用于"取多数"兜底）
    # big_pool  ：该项目在**同一大类**下出现过的组套（更可信，优先用）
    known = defaultdict(set)
    known_in_big = defaultdict(set)
    group_freq = Counter()          # 各组套在全表出现的次数，用于多数表决
    for r in rows:
        c5, big, nm, sg = map(_s, r)
        if not c5 or c5 in FAKE_GROUP:
            continue
        group_freq[c5] += 1
        keys = [k for k in (core_name(nm), sg) if k]
        for k in keys:
            known[k].add(c5)
            known_in_big[(big, k)].add(c5)

    recs = []
    for r in rows:
        c5, big, nm, sg = map(_s, r)
        if not nm:
            continue

        prof = profession(big)
        prof_src = "大类前缀"
        # 大类为空、或前缀没命中任何规则（都返回"未分类"）→ 走品名推断
        if not prof or prof == "未分类":
            prof = profession_guess(big, nm, sg)
            prof_src = "品名推断" if prof != "未分类" else "未识别"
        group = c5 if c5 and c5 not in FAKE_GROUP else ""
        filled = "原值"
        if not group:
            # ① 同大类内唯一命中 → 最可信（同一平台按同一组套归类）
            cands = set()
            for key in (core_name(nm), sg):
                if key:
                    cands |= known_in_big.get((big, key), set())
            if len(cands) == 1:
                group, filled = next(iter(cands)), "回填"
            else:
                # ② 全表范围内唯一命中
                cands = set()
                for key in (core_name(nm), sg):
                    if key:
                        cands |= known.get(key, set())
                if len(cands) == 1:
                    group, filled = next(iter(cands)), "回填"
                elif len(cands) > 1:
                    # ③ 有分歧（同项目在不同大类挂不同组套）→ 按全表出现次数取多数
                    #    例如 hs-CRP 同时出现在「炎症类/免疫」与「特殊蛋白类/生化」，
                    #    取记录数多的那个，避免被少数派带偏。
                    cnt = Counter()
                    for key in (core_name(nm), sg):
                        for g in known.get(key, set()):
                            cnt[g] += group_freq.get(g, 0)
                    if cnt:
                        best = max(cnt.items(), key=lambda kv: kv[1])[0]
                        group, filled = best, "回填(取多数)"
            # ④ 上面都救不回来，但所属大类本身就是独立检测领域 → 用大类立组套
            if not group and big in BIG_AS_GROUP:
                group, filled = BIG_AS_GROUP[big], "按大类立组"
        kind = "非检测项目" if is_nontest(nm, sg, big) else "检测项目"
        if not group and kind == "检测项目":
            group = "未归组"

        recs.append({
            "专业": prof,
            "组套": group,
            "品名": nm,
            "产品简称": sg,
            "大类": big,
            "项目性质": kind,
            "组套来源": filled,
            "专业来源": prof_src,
            "匹配键": " | ".join(x for x in (core_name(nm), sg) if x),
        })

    # ---- 合并同义词：同一专业+组套下把重复品名收敛 ----
    return recs


def summary(recs):
    print("=" * 62)
    print(f"总记录{len(recs)} 条")
    print("=" * 62)
    print("\n【第一层 · 专业】")
    for k, n in Counter(x["专业"] for x in recs).most_common():
        groups = len({x["组套"] for x in recs if x["专业"] == k and x["组套"]})
        print(f"   {k:12s} {n:5d} 条   下辖 {groups} 个组套")
    print("\n【第二层 · 组套】共 %d 个" % len({x["组套"] for x in recs}))
    for k, n in Counter(x["组套"] for x in recs).most_common(15):
        print(f"   {k:20s} {n:5d}")
    print("\n【组套来源】")
    for k, n in Counter(x["组套来源"] for x in recs).most_common():
        print(f"   {k:6s} {n:5d}")
    print("\n【专业来源】")
    for k, n in Counter(x["专业来源"] for x in recs).most_common():
        print(f"   {k:12s} {n:5d}")
    print("\n【项目性质】")
    for k, n in Counter(x["项目性质"] for x in recs).most_common():
        print(f"   {k:12s} {n:5d}")
    ungrouped = [x for x in recs if x["组套"] == "未归组"]
    print(f"\n【待人工确认】未归组 {len(ungrouped)} 条（均为检测项目）")
    for x in ungrouped[:12]:
        print(f"   [{x['专业']:6s}] {x['品名'][:44]:46s} {x['产品简称']}")


HEADERS = ["专业", "组套", "品名", "产品简称", "大类", "项目性质", "组套来源", "专业来源", "匹配键"]
WIDTHS = [10, 18, 48, 16, 18, 12, 14, 12, 56]


def write_xlsx(recs, out: Path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "项目分类表"

    head_fill = PatternFill("solid", fgColor="1F4E79")
    head_font = Font(color="FFFFFF", bold=True, size=11)
    ws.append(HEADERS)
    for i, h in enumerate(HEADERS, 1):
        c = ws.cell(row=1, column=i)
        c.fill, c.font = head_fill, head_font
        c.alignment = Alignment(horizontal="center", vertical="center")
    ws.freeze_panes = "A2"

    # 按专业 → 组套 排序，便于人工查阅
    order = {"生化": 0, "免疫": 1, "临检": 2, "分子": 3, "数字PCR": 4,
             "未分类": 9, "非检测项目": 8}
    recs2 = sorted(recs, key=lambda x: (order.get(x["专业"], 5),
                                       x["组套"], x["品名"]))
    for r in recs2:
        ws.append([r[h] for h in HEADERS])

    for i, w in enumerate(WIDTHS, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(vertical="center")
    ws.auto_filter.ref = f"A1:{get_column_letter(len(HEADERS))}{ws.max_row}"

    # 汇总页
    s2 = wb.create_sheet("分类汇总")
    s2.append(["层级", "名称", "记录数"])
    for c in s2[1]:
        c.fill, c.font = head_fill, head_font
    for prof, n in Counter(x["专业"] for x in recs2).most_common():
        s2.append(["第一层·专业", prof, n])
    for grp, n in Counter(x["组套"] for x in recs2).most_common():
        s2.append(["第二层·组套", grp, n])
    s2.column_dimensions["A"].width = 16
    s2.column_dimensions["B"].width = 26
    s2.column_dimensions["C"].width = 10

    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(out))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", nargs="?", default=str(SRC_DEFAULT))
    ap.add_argument("-o", "--out", default="项目分类表.xlsx")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    recs = build(Path(a.src))
    summary(recs)
    if a.dry_run:
        print("\ndry-run：未写文件")
        return
    out = Path(a.out)
    write_xlsx(recs, out)
    print(f"\n已生成：{out.resolve()}")


if __name__ == "__main__":
    main()