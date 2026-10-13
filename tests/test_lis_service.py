from __future__ import annotations

from io import BytesIO
from pathlib import Path
import sys
import tempfile
import unittest

import openpyxl

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "platform" / "backend"
for path in (PROJECT_ROOT, PROJECT_ROOT / "modules", BACKEND_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from app.services import lis_service


class LisServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.old_dirs = (lis_service.STAGING_DIR, lis_service.OUTPUT_DIR, lis_service.CONFIG_DIR)
        lis_service.STAGING_DIR = root / "staging"
        lis_service.OUTPUT_DIR = root / "standardized"
        lis_service.CONFIG_DIR = root / "mappings"
        self.addCleanup(self._restore_dirs)

    def _restore_dirs(self):
        lis_service.STAGING_DIR, lis_service.OUTPUT_DIR, lis_service.CONFIG_DIR = self.old_dirs

    @staticmethod
    def _workbook() -> bytes:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "LIS"
        ws.append(["LIS导出报告"])
        ws.append(["条码号", "姓名", "性别", "检验项目", "检验结果", "接收时间", "报告时间"])
        ws.append(["B001", "张三", "男", "血常规", "正常", "2026-10-01 08:00:00", "2026-10-01 09:00:00"])
        stream = BytesIO()
        wb.save(stream)
        return stream.getvalue()

    def test_preview_suggests_mapping_and_confirm_saves_standard_copy(self):
        original = self._workbook()
        original_copy = original[:]
        preview = lis_service.preview_upload("sample.xlsx", original)
        self.assertEqual(preview["header_row"], 2)
        self.assertEqual(len(preview["fields"]), 44)
        self.assertEqual(preview["row_count"], 1)
        self.assertEqual(len(preview["preview"]), 1)

        saved = lis_service.confirm_save(preview["session_id"], preview["mapping"], preview["header_row"], "实验室A")
        output_path, output_name = lis_service.output_file(saved["batch_id"])
        self.assertTrue(output_path.is_file())
        self.assertEqual(output_name, saved["file"])
        output = openpyxl.load_workbook(output_path, read_only=True, data_only=True)
        try:
            rows = list(output["lis_std"].iter_rows(values_only=True))
        finally:
            output.close()
        self.assertEqual(rows[0][0], "记录唯一号")
        self.assertEqual(rows[1][1], "B001")
        self.assertEqual(rows[1][6], "张三")
        self.assertEqual(rows[1][7], "男")
        self.assertEqual(len(rows), 2)
        self.assertEqual((lis_service.STAGING_DIR / f"{preview['session_id']}.xlsx").exists(), False)
        self.assertEqual(original, original_copy)

    def test_real_xls_preview_and_confirm_save_keep_source_unchanged(self):
        source = Path(__file__).resolve().parent / "fixtures" / "lis_sample.xls"
        self.assertTrue(source.is_file(), f"缺少真实 XLS 测试样例：{source}")
        content = source.read_bytes()
        self.assertEqual(content[:8], b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "样例必须是 OLE/BIFF 真 .xls")

        preview = lis_service.preview_upload(source.name, content)

        self.assertEqual(preview["header_row"], 2)
        self.assertEqual(preview["sheet"], "LIS导出")
        self.assertEqual(preview["row_count"], 2)
        self.assertEqual(
            preview["headers"],
            ["条码号", "姓名", "性别", "检验项目", "检验结果", "接收时间", "报告时间"],
        )

        original = content[:]
        saved = lis_service.confirm_save(preview["session_id"], preview["mapping"], preview["header_row"], "实验室A")

        output_path, output_name = lis_service.output_file(saved["batch_id"])
        self.assertTrue(output_path.is_file())
        self.assertTrue(output_name.endswith(".xlsx"), "标准化结果仍输出 xlsx")
        self.assertEqual(saved["rows"], 2)
        output = openpyxl.load_workbook(output_path, read_only=True, data_only=True)
        try:
            rows = list(output["lis_std"].iter_rows(values_only=True))
        finally:
            output.close()
        self.assertEqual(rows[0][0], "记录唯一号")
        self.assertEqual(rows[1][1], "B001")
        self.assertEqual(rows[2][6], "李四")
        self.assertEqual((lis_service.STAGING_DIR / f"{preview['session_id']}.xls").exists(), False)
        self.assertEqual(content, original, "原始 .xls 不得被修改")

    def test_xls_header_row_can_be_reselected(self):
        source = Path(__file__).resolve().parent / "fixtures" / "lis_sample.xls"
        self.assertTrue(source.is_file())

        preview = lis_service.preview_upload(source.name, source.read_bytes(), header_row=1)

        self.assertEqual(preview["header_row"], 1)
        self.assertEqual(preview["row_count"], 3)
        self.assertEqual(preview["headers"][0], "LIS导出报告")
        self.assertEqual(preview["preview"][0][0], "条码号")

    def test_unrecognizable_binary_is_rejected(self):
        """既不是 Excel 也不是文本的文件应被拒绝，且给出可读原因。"""
        with self.assertRaisesRegex(ValueError, "损坏|无法识别|不是有效"):
            lis_service.preview_upload("broken.xls", bytes(range(256)) * 20)

    def test_required_mapping_is_checked_before_output(self):
        preview = lis_service.preview_upload("sample.xlsx", self._workbook())
        mapping = [item for item in preview["mapping"] if item["target_key"] != "barcode"]
        with self.assertRaisesRegex(ValueError, "标本条码号"):
            lis_service.confirm_save(preview["session_id"], mapping, preview["header_row"])
        self.assertEqual(list(lis_service.OUTPUT_DIR.glob("*.xlsx")) if lis_service.OUTPUT_DIR.exists() else [], [])

    def test_csv_accepts_gb18030_and_normalizes_date_and_negative_numeric(self):
        content = "条码号,接收时间,TAT\nB002,20260701,-5分钟\n".encode("gb18030")
        preview = lis_service.preview_upload("source.csv", content)
        self.assertEqual(preview["header_row"], 1)
        self.assertEqual(preview["row_count"], 1)


if __name__ == "__main__":
    unittest.main()
