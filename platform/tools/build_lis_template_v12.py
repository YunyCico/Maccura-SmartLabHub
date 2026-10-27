# -*- coding: utf-8 -*-
"""按梦云 2026-10-20 的要求改造 LIS 数据标准模板。

三件事：
1. ~~删掉 6 个结果值列~~ → 梦云改为「不删，只是预览时没有信息的不体现」，
   字段一个不动，44 列全保留；空列的隐藏交给前端预览层处理。
2. 取消必填：所有字段必填列一律改成「否」，不再有必填拦截。
3. 新增「专业」「组套」两列，由检测项目名称 / 项目英文名称对照试剂分类表自动填。

★ 只改副本，先备份；正式模板要人工确认后再覆盖。
"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
import shutil
import sys

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

DESKTOP_TEMPLATE = Path(r"C:\Users\98319\Desktop\LIS数据标准模板_中文列名.xlsx")
DESKTOP_TAXONOMY = Path(r"C:\Users\98319\Desktop\试剂分类20261019.xlsx")
PROJECT_TEMPLATE = Path(
    r"C:\Users\98319\Desktop\工作平台\SmartLabHub\platform\backend\app\resources\LIS数据标准模板_中文列名.xlsx"
)
PROJECT_TAXONOMY = Path(
    r"C:\Users\98319\Desktop\工作平台\SmartLabHub\platform\backend\app\resources\LIS项目分类表.xlsx"
)

# 2026-10-20 决定：结果值列全部保留，不删
DROPPED_KEYS: list[str] = []

# 新增的两列分类字段（紧跟检验仪器之后，属于 D标本检测 组）
NEW_FIELDS = [
    ["专业", "test_specialty", "D标本检测", "否", "文本", "按项目名称对照试剂分类表自动填充", "试剂分类表·专业"],
    ["组套", "test_suite", "D标本检测", "否", "文本", "按项目名称对照试剂分类表自动填充", "试剂分类表·组套"],
]

HEAD_FILL = PatternFill("solid", fgColor="1B5FAA")
HEAD_FONT = Font(color="FFFFFF", bold=True)
THIN = Side(style="thin", color="D5E0ED")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def read_taxonomy() -> list[dict]:
    """从试剂分类表读取 专业/组套/品名/产品简称。"""
    wb = openpyxl.load_workbook(DESKTOP_TAXONOMY, read_only=True, data_only=True)
    try:
        ws = wb["组套核对"]
        rows = list(ws.iter_rows(min_row=2, values_only=True))
    finally:
        wb.close()
    out = []
    for row in rows:
        if not row or not row[0]:
            continue
        out.append({
            "专业": str(row[0]).strip(),
            "组套": str(row[1]).strip() if row[1] else "",
            "品名": str(row[2]).strip() if row[2] else "",
            "产品简称": str(row[3]).strip() if row[3] else "",
        })
    return out


def collect_kept_fields(wb) -> list[list]:
    """从「字段说明」读取他保留的字段，顺序即最终列序；必填一律改成「否」。"""
    ws = wb["字段说明"]
    kept = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or not row[0] or str(row[0]).strip() == "列序号":
            continue
        key = str(row[2]).strip()
        if not key or key in DROPPED_KEYS:
            continue
        kept.append([str(row[1]).strip(), key, str(row[3]).strip(), "否",
                     str(row[5]).strip(), str(row[6]).strip(), str(row[7]).strip()])
    return kept


def rewrite_field_dict(ws, fields) -> None:
    ws.delete_rows(1, ws.max_row)
    ws.append(["列名(中文)", "英文字段名", "分组", "必填", "类型", "说明", "原始数据来源"])
    for item in fields:
        ws.append(item)
    style_header(ws, 7)


def rewrite_field_desc(ws, fields) -> None:
    ws.delete_rows(1, ws.max_row)
    ws.append(["列序号", "列名(中文)", "英文字段名", "分组", "必填", "类型", "说明", "原始数据来源"])
    for index, item in enumerate(fields, start=1):
        ws.append([index] + item)
    style_header(ws, 8)
    ws.column_dimensions["A"].width = 8


def rewrite_lis_std(ws, fields) -> None:
    ws.delete_rows(1, ws.max_row)
    ws.append([item[0] for item in fields])
    style_header(ws, len(fields))


def rewrite_sample(ws, fields) -> list:
    ws.delete_rows(1, ws.max_row)
    labels = [item[0] for item in fields]
    ws.append(labels)
    sample = {
        "记录唯一号": "R0000001", "标本条码号": "262601310925",
        "医嘱申请单号": "SO20260301-0001", "检验单编号": "TO20260301-0001",
        "项目结果编号": "RES0000001", "样本编号": "305",
        "患者姓名": "鲁忠才", "患者性别": "男", "年龄原文": "77岁",
        "年龄(岁)": 77, "病历号": "2026012824", "就诊类型": "住院",
        "申请科室": "泌尿外科住院", "病区名称": "新院区", "床号": "045",
        "开单医生": "贾兴泽", "临床诊断": "腰痛", "送检目的": "常规",
        "标本种类": "血清", "检验小组": "迈克MAX生免流水线",
        "检测项目名称": "白蛋白", "项目编码": "10070",
        "项目英文名称": "ALB", "检验仪器": "LABASMIX_1",
        "结果数值": 33.7, "结果文本": "33.7", "结果单位": "g/L",
        "参考范围": "35-55", "异常标记": "偏低", "异常箭头": "↓",
        "专业": "生化", "组套": "肝功类",
        "申请时间": "2026-02-01 07:50:00", "采集时间": "2026-02-01 07:57:09",
        "接收时间": "2026-02-01 08:34:28", "上机时间": "2026-02-01 09:10:00",
        "审核时间": "2026-02-01 09:19:27", "报告时间": "2026-02-01 09:19:27",
        "LIS自带TAT(分钟)": 45, "来源实验室": "佳佳",
        "来源文件": "样本周转时间查询2026-03-19.xlsx", "来源工作表": "sheet1",
        "表头行号": 1, "来源行号": 2, "导入批次号": "B20260307-001",
    }
    ws.append([sample.get(label) for label in labels])
    style_header(ws, len(labels))
    for index in range(1, len(labels) + 1):
        ws.cell(row=2, column=index).border = BORDER
    return labels


def rewrite_dict_values(ws) -> None:
    """枚举对照表：字段全部保留，只补下拉。"""
    kept = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or not row[0]:
            continue
        kept.append([row[0], row[1], row[2]])
    ws.delete_rows(1, ws.max_row)
    ws.append(["字段(中文)", "原始值", "标准值"])
    for item in kept:
        ws.append(item)
    style_header(ws, 3)
    for label in {item[0] for item in kept}:
        values = _enum_values(kept, label)
        if not values:
            continue
        dv = DataValidation(type="list", formula1=f'"{values}"', allow_blank=True)
        ws.add_data_validation(dv)
        dv.add(f"C2:C{max(2, ws.max_row)}")


def _enum_values(rows, label) -> str:
    values = []
    for item in rows:
        if item[0] == label and item[2] and item[2] not in values:
            values.append(str(item[2]))
    return ",".join(values)


def rewrite_quality_rules(ws) -> None:
    """取消「拒绝该行」类强制规则，改为记录提示。"""
    ws.delete_rows(1, ws.max_row)
    ws.append(["校验类别", "规则", "不通过时处理"])
    rows = [
        ["主键完整性", "标本条码号 为空", "留空并记入提示（不拒绝该行）"],
        ["主键完整性", "检测项目名称 为空", "留空并记入提示（不拒绝该行）"],
        ["时间顺序", "采集时间 ≤ 接收时间", "违反记入提示"],
        ["时间顺序", "接收时间 ≤ 报告时间", "违反记入提示"],
        ["时间合理性", "报告时间 ≥ 采集时间", "违反记入提示"],
        ["时间合理性", "LIS自带TAT(分钟) < 0", "置空，不参与统计"],
        ["时间合理性", "TAT 列文本如「分钟」「179分钟」", "正则提取数字，失败置空"],
        ["未来时间", "任何时间戳不得晚于导入时间+1天", "记入提示"],
        ["日期格式", "20260701 / 20250401 / 25.09.01", "统一为 YYYY-MM-DD，缺前缀补 20"],
        ["重复行", "标本条码号 + 项目编码 重复", "保留最后一条并记录"],
        ["文件格式", "扩展名与 magic 不符（如 xlsx 实为 xls）", "按 magic 判断并走对应解析器"],
        ["表头定位", "表头行号由映射表 header_row 指定", "不得默认取第 1 行"],
        ["项目分类", "检测项目名称 / 项目英文名称 对照试剂分类表", "命中填专业+组套，未命中留空"],
    ]
    for item in rows:
        ws.append(item)
    style_header(ws, 3)


def rewrite_readme(ws, fields) -> None:
    ws.delete_rows(1, ws.max_row)
    rows = [
        ["LIS 数据标准模板 v1.2（中文列名版 · 取消必填 · 含项目分类）", ""],
        ["三层结构", "原始层(只读保留) → 标准层(本模板) → 分析层(TAT/设备负载取子集)"],
        ["列名语言", "模板表头一律用中文；英文字段名见 field_dict，仅供程序使用"],
        ["粒度", "标准层一行 = 一个检测项目结果"],
        ["主键", "标本条码号 是跨表关联的唯一依据"],
        ["填表原则", "★ 不设必填：源表有的就填，没有的留空，绝不拒绝整行"],
        ["预览规则", "★ 预览与导出时，某字段在全部数据行都为空则该列不体现"],
        ["项目分类", "专业 / 组套 两列由检测项目名称、项目英文名称对照试剂分类表自动填充"],
        ["工作表说明", ""],
        ["字段说明", f"{len(fields)} 列的序号/中文列名/英文字段名/分组/必填/说明/来源"],
        ["lis_std", "★标准数据模板，第1行中文列名，勿加标题行，直接填数据"],
        ["field_dict", "中英对照字典；英文字段名是程序权威 key，勿改"],
        ["field_mapping", "已实测的映射关系，新增实验室只加行不动表结构"],
        ["dict_values", "性别/就诊类型枚举对照（标准值也是中文）"],
        ["sample_data", "1 行示例，含项目分类填充效果"],
        ["quality_rules", "导入后执行的校验规则，全部为提示级，不拒绝行"],
        ["时间字段硬规则", ""],
        ["格式", "统一 YYYY-MM-DD HH:MM:SS"],
        ["缺失", "留空，禁止填 0 或当前时间"],
        ["负TAT", "置空并记入异常表，不参与统计"],
        ["实测样本", "佳佳 2 份 / 孟鹏杰 1 份 / 爱成 3 份，共约 395MB"],
        ["已知坑", "xlsx 扩展名实为 xls、表头在第 3-4 行、日期三种写法、TAT 列混文本"],
        ["v1.2 变更", "取消全部必填；新增专业/组套分类列；预览与导出隐藏全空列"],
    ]
    for item in rows:
        ws.append(item)
    ws.column_dimensions["A"].width = 16
    ws.column_dimensions["B"].width = 92
    for row in ws.iter_rows(min_row=1, max_row=ws.max_row, max_col=2):
        row[0].font = Font(bold=True)
        row[0].alignment = Alignment(vertical="center")
        row[1].alignment = Alignment(wrap_text=True, vertical="center")


def style_header(ws, width: int) -> None:
    for index in range(1, width + 1):
        cell = ws.cell(row=1, column=index)
        cell.fill = HEAD_FILL
        cell.font = HEAD_FONT
        cell.alignment = Alignment(vertical="center", horizontal="center")
        cell.border = BORDER
        ws.column_dimensions[get_column_letter(index)].width = 16
    ws.freeze_panes = "A2"


def main() -> int:
    if not DESKTOP_TEMPLATE.is_file():
        print("找不到桌面模板", DESKTOP_TEMPLATE)
        return 1
    if not DESKTOP_TAXONOMY.is_file():
        print("找不到试剂分类表", DESKTOP_TAXONOMY)
        return 1

    taxonomy = read_taxonomy()
    print(f"试剂分类表读取：{len(taxonomy)} 行")

    backup = DESKTOP_TEMPLATE.with_name(DESKTOP_TEMPLATE.stem + "_改前备份.xlsx")
    if not backup.exists():
        shutil.copy2(DESKTOP_TEMPLATE, backup)
        print("已备份 ->", backup.name)

    wb = openpyxl.load_workbook(DESKTOP_TEMPLATE)
    kept = collect_kept_fields(wb)
    print(f"字段说明保留 {len(kept)} 列（结果值列全部保留）")

    labels = [item[0] for item in kept]
    insert_at = labels.index("检验仪器") + 1 if "检验仪器" in labels else len(labels)
    fields = kept[:insert_at] + [list(item) for item in NEW_FIELDS] + kept[insert_at:]
    print(f"新增分类列后共 {len(fields)} 列，专业/组套插在第 {insert_at + 1}、{insert_at + 2} 位")

    rewrite_field_desc(wb["字段说明"], fields)
    rewrite_field_dict(wb["field_dict"], fields)
    rewrite_lis_std(wb["lis_std"], fields)
    rewrite_sample(wb["sample_data"], fields)
    rewrite_dict_values(wb["dict_values"])
    rewrite_quality_rules(wb["quality_rules"])
    rewrite_readme(wb["readme"], fields)

    wb.save(DESKTOP_TEMPLATE)
    print("已保存桌面模板")

    shutil.copy2(DESKTOP_TEMPLATE, PROJECT_TEMPLATE)
    print("已同步到项目 resources")
    shutil.copy2(DESKTOP_TAXONOMY, PROJECT_TAXONOMY)
    print("试剂分类表已同步到项目 resources")
    return 0


if __name__ == "__main__":
    sys.exit(main())