# -*- coding: utf-8 -*-
"""LIS 数据整理：确认阶段必须沿用「预览时选中的工作表」——回归验证。

★ 这个坑踩过两次，值得钉死。

背景：
    预览阶段 `_read_workbook(content, filename, selected_sheet)` 按用户选的工作表读；
    确认阶段 `_standard_payload` 曾经**不传 sheet**，于是它把工作簿里所有表都读一遍，
    再按「列数多、行数多」自动挑一个 —— 挑到的往往不是用户选的那张。
    结果：字段映射里的来源列在表头里找不到，报「来源字段不存在：xxx」。
    实测《标本明细》17 个表：用户选「生化」，自动挑中「2026.07数据」，
    前者的 TAT 列叫「采样-报告」、后者叫「采集-报告」，就差一个字就报错。

用法：
    python platform/tools/verify_lis_sheet_consistency.py
    （不传参时用真实数据源跑；设 SLH_LIS_DIR 可把暂存目录隔离到临时目录）
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = PROJECT_ROOT / "platform" / "backend"
for path in (PROJECT_ROOT, BACKEND_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

# 隔离暂存目录：别往用户真实的 modules/data_analysis/data/lis/ 写垃圾
os.environ.setdefault("SLH_LIS_DIR", tempfile.mkdtemp(prefix="slh_lis_verify_"))

from app.services import lis_service as lis_service  # noqa: E402
from modules.data_analysis import smartlab_engine as engine  # noqa: E402


def check_picked_sheet() -> bool:
    """单元级：多工作表时，确认阶段不能挑错表。"""
    from io import BytesIO

    import openpyxl
    wb = openpyxl.Workbook()
    wide = wb.active
    wide.title = "列多的表"
    wide.append(["条码号", "姓名", "性别", "年龄", "检验项目", "检验结果", "接收时间", "报告时间"])
    wide.append(["B001", "张三", "男", "41", "血常规", "正常", "2026-10-01 08:00:00", "2026-10-01 09:00:00"])
    narrow = wb.create_sheet("用户选的那张")
    narrow.append(["条码号", "姓名", "检验项目", "采样-报告"])
    narrow.append(["B002", "李四", "血常规", "35"])
    stream = BytesIO()
    wb.save(stream)
    content = stream.getvalue()

    preview = lis_service.preview_upload("multi.xlsx", content, None, "用户选的那张")
    assert preview["sheet"] == "用户选的那张", preview["sheet"]
    assert "采样-报告" in preview["headers"]
    result = lis_service.preview_standard(
        preview["session_id"], preview["mapping"], preview["header_row"], "")
    assert result["total_rows"] == 1, result["total_rows"]
    print("  ✅ 多工作表：确认阶段沿用了预览时选中的表（总行数 %d）" % result["total_rows"])
    return True


def check_real_sources(full: bool = False) -> bool:
    """真实数据源：逐个工作表跑一遍「读取 → 确认预览」。

    默认每个数据集只测行数最少的那张表（几十秒内跑完，适合日常回归）；
    加 --full 则把所有工作表都测一遍（十几分钟）。
    """
    engine.load_state()
    datasets = engine.STATE.get("datasets") or []
    if not datasets:
        print("  ⚠️  当前没有已导入的数据源，跳过真实数据验证")
        return True
    ok = True
    for dataset in datasets:
        sheets = [item for item in dataset.get("sheets", []) if item.get("name")]
        if not sheets:
            continue
        if full:
            picked = sheets
        else:
            # 空表没有可整理的内容，跳过；否则会撞上"空表"提示而被误判成失败
            usable = [item for item in sheets if int(item.get("rows") or 0) > 0]
            picked = [min(usable, key=lambda item: int(item.get("rows") or 0))] if usable else []
        for sheet in picked:
            started = time.time()
            try:
                preview = lis_service.preview_imported_dataset(dataset["id"], sheet["name"])
                result = lis_service.preview_standard(
                    preview["session_id"], preview["mapping"], preview["header_row"], "")
                print("  ✅ %s / %s：%d 行、%d 个有效列、%d 条质量提示（%.1f 秒）"
                      % (dataset["name"][:28], sheet["name"], result["total_rows"],
                         len(result["columns"]), result["quality_issue_count"],
                         time.time() - started))
            except Exception as error:      # noqa: BLE001
                ok = False
                print("  ❌ %s / %s：%s" % (dataset["name"][:28], sheet["name"], error))
    return ok


def check_source_not_deleted() -> bool:
    """保存后不能把库里的源文件删了。"""
    engine.load_state()
    dataset = next((item for item in engine.STATE.get("datasets") or []
                    if item.get("sheets")), None)
    if dataset is None:
        print("  ⚠️  没有可用数据源，跳过源文件保护验证")
        return True
    source = engine.ds_abs_path(dataset)
    before = (os.path.getsize(source), os.path.getmtime(source))
    preview = lis_service.preview_imported_dataset(dataset["id"], dataset["sheets"][0]["name"])
    lis_service.confirm_save(preview["session_id"], preview["mapping"], preview["header_row"], "")
    after = (os.path.getsize(source), os.path.getmtime(source))
    assert before == after, "源文件被改动了！"
    print("  ✅ 保存标准化结果后，源文件完好（大小与修改时间均未变）")
    return True


def main() -> int:
    full = "--full" in sys.argv
    print("=" * 76)
    print("LIS 工作表一致性回归验证%s" % ("（全量）" if full else "（快速，每个数据集一张表）"))
    print("=" * 76)
    print("\n[1] 多工作表选择")
    check_picked_sheet()
    print("\n[2] 真实数据源逐表验证")
    real_ok = check_real_sources(full)
    print("\n[3] 源文件保护")
    check_source_not_deleted()
    print("\n" + ("全部通过 ✅" if real_ok else "存在失败 ❌"))
    return 0 if real_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
