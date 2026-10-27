# -*- coding: utf-8 -*-
"""组套兜底匹配：处理「套餐名」类检测项目。

问题：LIS 导出的「检测项目」里，高频项是**组套/套餐名**（肝功六项、凝血四项、
电解质四项…），而试剂分类表里只有单项（ALT、GGT…）。纯名称匹配必然漏掉。

办法：在精确匹配之后，加一层**组套关键词**兜底 —— 从项目名里认出它属于哪个组套。
例：肝功六项 → 含「肝功」→ 肝功类。

两级匹配顺序：
    1. 精确/模糊匹配匹配键（品名主干 + 简称变体）→ 最准
    2. 组套关键词兜底 → 覆盖套餐名
    3. 都失败 → 归「未分类」，界面要标出来而不是静默归类

用法：
    python platform/tools/match_taxonomy.py --sample
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from pathlib import Path

import openpyxl

TAXONOMY = Path(__file__).resolve().parents[2] / "modules" / "data_analysis" / "data" / "taxonomy" / "项目分类表.xlsx"

# ---- 组套关键词兜底表：关键词 → 组套名 ----
# 只收「辨识度高、误伤小」的词。像「蛋白」「酶」这种太泛的不要。
GROUP_KEYWORDS = {
    # 生化
    "肝功": "肝功类", "肝功能": "肝功类", "转氨酶": "肝功类",
    "肾功": "肾功类", "肾功能": "肾功类", "尿素": "肾功类", "肌酐": "肾功类",
    "血脂": "血脂及脂蛋白类", "胆固醇": "血脂及脂蛋白类", "甘油三酯": "血脂及脂蛋白类",
    "心肌酶": "心肌标志物类", "心肌": "心肌标志物类", "肌钙蛋白": "心肌标志物类", "Tn": "心肌标志物类",
    "电解质": "电解质类", "血糖": "糖代谢类", "糖化": "糖化类", "糖代谢": "糖代谢类",
    "胆红素": "肝功类", "肿瘤标志物": "肿瘤类", "肿瘤": "肿瘤类",
    "特殊蛋白": "特殊蛋白类", "淀粉样蛋白": "炎症类", "hs-CRP": "炎症类",
    "胰腺": "胰腺类", "淀粉酶": "胰腺类",
    # 免疫
    "乙肝": "传染病类", "丙肝": "传染病类", "梅毒": "传染病类", "艾滋": "传染病类",
    "HIV": "传染病类", "HBV": "传染病类", "HCV": "传染病类", "新冠": "传染病类",
    "甲功": "甲状腺功能类", "甲状腺": "甲状腺功能类", "TSH": "甲状腺功能类",
    "性激素": "性激素类", "女性激素": "性激素类", "孕酮": "性激素类", "雌二醇": "性激素类",
    "唐筛": "唐筛类", "产前筛查": "唐筛类", "NIPT": "优生优育类", "优生优育": "优生优育类",
    "类风湿": "自身免疫类", "自身抗体": "自身免疫类", "ANA": "自身免疫类",
    "过敏原": "过敏原类", "维生素D": "骨代谢标志物类", "骨代谢": "骨代谢标志物类",
    "神经": "神经相关标志物类", "神经元": "神经相关标志物类", "肌电图": "神经相关标志物类",
    "醛固酮": "高血压类", "高血压": "高血压类", "肾素": "高血压类",
    # 临检
    "凝血": "常规凝血类", "PT": "常规凝血类", "APTT": "常规凝血类", "纤维蛋白原": "常规凝血类",
    "D-二聚体": "纤溶类", "二聚体": "纤溶类", "FDP": "纤维蛋白形成及降解产物类",
    "血常规": "血球类", "全血细胞": "血球类", "五分类": "血球类", "血细胞计数": "血球类",
    "尿液": "尿液类", "尿常规": "尿液类", "尿蛋白": "尿液类",
    "血型": "输血类", "输血": "输血类", "交叉配血": "输血类", "Rh": "输血类", "ABO": "输血类",
    "疟原虫": "血液寄生虫类", "血型鉴定": "输血类",
    # 分子/PCR
    "核酸": "呼吸道类", "DNA": "性病类", "RNA": "呼吸道类",
    "HPV": "性病类", "梅毒螺旋体": "性病类", "淋球菌": "性病类",
    "呼吸道病原体": "呼吸道类", "支原体": "呼吸道类", "新型冠状病毒": "呼吸道类",
    "基因": "基因提取类", "基因提取": "基因提取类",
    "消化道": "消化道类", "幽门螺杆菌": "消化道类", "沙门氏菌": "消化道类",
    "细胞治疗": "细胞治疗类", "药物基因": "药物基因类", "遗传病": "遗传病类",
}

# 「LIS 里写这套名字但试剂表没收录」的常见别名 → 归到最近组套
EXTRA_ALIAS = {
    "肝功": "肝功类", "肾功": "肾功类", "血脂": "血脂及脂蛋白类",
    "心肌酶": "心肌标志物类", "凝血": "常规凝血类",
}


def load(taxonomy: Path):
    """读去重后的分类表，返回 (匹配键→(专业,组套), 组套集合, 组套→专业)。"""
    wb = openpyxl.load_workbook(taxonomy, data_only=True)
    ws = wb[wb.sheetnames[0]]
    exact = {}
    group_prof = {}
    groups = set()
    for r in ws.iter_rows(min_row=2, values_only=True):
        prof, grp, _nm, _sg, key = (list(r) + [""] * 5)[:5]
        prof = (prof or "").strip()
        grp = (grp or "").strip()
        if grp:
            groups.add(grp)
            group_prof.setdefault(grp, prof)
        for k in str(key or "").split("|"):
            k = k.strip()
            if k and k not in exact:
                exact[k] = (prof, grp)
    return exact, groups, group_prof


def match(value: str, exact: dict, groups: set, group_prof: dict):
    """两级匹配。返回 (专业, 组套, 命中方式)。"""
    v = (value or "").strip()
    if not v:
        return ("", "", "空值")

    # ① 精确
    if v in exact:
        p, g = exact[v]
        return (p, g, "精确")

    # ② 模糊：匹配键是 v 的子串或 v 是匹配键的子串（取最长的那个）
    best = None
    for k, (p, g) in exact.items():
        if len(k) < 2:
            continue
        if k in v or v in k:
            if best is None or len(k) > len(best[0]):
                best = (k, p, g)
    if best:
        return (best[1], best[2], f"模糊({best[0][:12]})")

    # ③ 组套关键词兜底 —— 专门救"套餐名"
    for kw, grp in GROUP_KEYWORDS.items():
        if kw in v and grp in groups:
            return (group_prof.get(grp, ""), grp, f"组套关键词[{kw}]")
    for kw, grp in EXTRA_ALIAS.items():
        if kw in v:
            return (group_prof.get(grp, ""), grp, f"别名[{kw}]")

    return ("未分类", "未分类", "未匹配")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--taxonomy", default=str(TAXONOMY))
    ap.add_argument("--sample", nargs="*", help="直接匹配给定字符串")
    a = ap.parse_args()

    exact, groups, gprof = load(Path(a.taxonomy))
    print(f"词典：{len(exact)} 个匹配键 / {len(groups)} 个组套\n")

    if a.sample:
        for s in a.sample:
            p, g, how = match(s, exact, groups, gprof)
            print(f"  {s[:38]:40s} → {p:8s} / {g:20s} [{how}]")
        return

    print("组套关键词兜底规则条数：", len(GROUP_KEYWORDS))
    print("提示：接上明细表可测覆盖率")


if __name__ == "__main__":
    main()