"""「假 .xls」识别回归：扩展名说 .xls，内容其实是 CSV / HTML。

背景（2026-10-10 真实故障）：用户导入 LIS 导出文件时报"导入失败"。
根因是这些文件扩展名是 `.xls`，内容却分别是：
  - `table_2.xls`                     → 带 BOM 的 UTF-8 中文 CSV（3.4MB）
  - `导出仪器工作量26年6-7月.xls`      → Excel「导出为网页」的 HTML 表格
  - `导出科室工作量26年6-7月.xls`      → 同上
按扩展名硬判 `.xls` → 一律报"不是有效的旧版 Excel 工作簿"。

修复口径：**读取一律按内容分派，扩展名只决定"要不要接"**。
"""
from pathlib import Path

import pytest

from modules.data_analysis import smartlab_engine as engine
from modules.data_analysis import table_format

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FAKE_CSV = FIXTURES / "fake_xls_chinese.csv.xls"
FAKE_HTML = FIXTURES / "fake_xls_html_table.xls"
REAL_XLS = FIXTURES / "lis_sample.xls"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    (lib / "sources").mkdir(parents=True)
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(engine, "LIB_DIR", str(lib))
    monkeypatch.setattr(engine, "OUT_DIR", str(out))
    monkeypatch.setattr(engine, "BAK_DIR", str(out / "_bak"))
    monkeypatch.setattr(engine, "STATE", {"datasets": [], "config": {}})
    engine._cache.clear()
    yield tmp_path
    engine._cache.clear()


def _import(tmp_path, source: Path, name: str):
    target = tmp_path / name
    target.write_bytes(source.read_bytes())
    return engine.import_file(str(target), copy=True, display_name=name)


def test_format_sniffer_identifies_each_real_world_shape():
    assert table_format.sniff_bytes(REAL_XLS.read_bytes()[:8192]) == table_format.FMT_OLE_XLS
    assert table_format.sniff_bytes(FAKE_CSV.read_bytes()[:8192]) == table_format.FMT_TEXT
    assert table_format.sniff_bytes(FAKE_HTML.read_bytes()[:8192]) == table_format.FMT_HTML


def test_chinese_csv_is_not_mistaken_for_binary():
    """中文 CSV 高位字节占 40%+，不能按"高位字节比例"判成二进制。"""
    head = FAKE_CSV.read_bytes()[:8192]
    ratio = sum(1 for b in head if b > 0x7F) / len(head)
    assert ratio > 0.3, "本样本高位字节占比确实超过 0.3，正是当初误判的阈值"
    assert table_format.sniff_bytes(head) == table_format.FMT_TEXT


def test_sampling_must_not_split_multibyte_character():
    """固定 8192 字节采样会把 UTF-8 汉字劈开，必须回退到字符边界。"""
    payload = ("病历号,姓名,送检时间\n" + "0001,张三,2026-04-23 08:30\n" * 400).encode("utf-8")
    for cut in (8192, 4096, 1024, 511):
        chunk = payload[:cut]
        assert table_format.sniff_bytes(chunk) == table_format.FMT_TEXT, f"cut={cut} 被误判"


def test_import_fake_xls_csv_content(isolated):
    ds = _import(isolated, FAKE_CSV, "table_2.xls")
    sheet = ds["sheets"][0]
    assert not sheet.get("error"), sheet.get("error")
    assert sheet["rows"] == 3
    assert sheet["columns"][:3] == ["病历号", "姓名", "条形码"]
    assert sheet["name"] == "CSV"


def test_import_fake_xls_html_content(isolated):
    ds = _import(isolated, FAKE_HTML, "导出科室工作量.xls")
    sheet = ds["sheets"][0]
    assert not sheet.get("error"), sheet.get("error")
    assert sheet["name"] == "HTML表格"
    assert sheet["columns"][:4] == ["院区", "组别", "仪器名称", "检查人次"]
    assert sheet["rows"] >= 1


def test_import_real_xls_still_works(isolated):
    """修假 xls 不能把真 xls 弄坏。"""
    ds = _import(isolated, REAL_XLS, "lis_sample.xls")
    sheet = ds["sheets"][0]
    assert not sheet.get("error"), sheet.get("error")
    assert sheet["name"] == "LIS导出"
    assert sheet["columns"][:3] == ["条码号", "姓名", "性别"]


def test_raw_preview_reads_fake_xls(isolated):
    """原始数据预览也要能看假 xls，否则用户导进来还是打不开。"""
    target = isolated / "预览.csv.xls"
    target.write_bytes(FAKE_CSV.read_bytes())
    assert engine.sheet_names(str(target)) == ["CSV"]
    rows = engine.cached_raw(str(target), "CSV")
    assert rows[0][:3] == ["病历号", "姓名", "条形码"]
    assert len(rows) == 4


def test_large_file_stream_import_handles_fake_xls(isolated, monkeypatch):
    monkeypatch.setattr(engine, "LARGE_FILE_BYTES", 1)
    ds = _import(isolated, FAKE_CSV, "大表.csv.xls")
    sheet = ds["sheets"][0]
    assert sheet["large"] is True
    assert sheet["rows"] == 3
    page = engine._large_page(ds, sheet, 0, 10)
    assert page[0][:3] == ["0001", "张三", "BC001"]


def test_genuinely_unknown_binary_is_rejected_with_reason(isolated):
    """真读不出来的文件要给能看懂的原因，不能是空泛的"导入失败"。"""
    bogus = isolated / "损坏.xls"
    bogus.write_bytes(bytes(range(256)) * 40)   # 既非 OLE 也非文本
    with pytest.raises(ValueError) as excinfo:
        engine.import_file(str(bogus), copy=False)
    assert "无法识别" in str(excinfo.value)