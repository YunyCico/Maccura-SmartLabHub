# -*- coding: utf-8 -*-
"""表格文件「真实格式」嗅探。

为什么需要：LIS/检验系统导出时经常把 CSV 或 HTML 表格直接存成 `.xls`。
实测样本（2026-10-10）：

  | 文件名 | 扩展名声称 | 实际内容 |
  | --- | --- | --- |
  | `table_2.xls` | .xls | 带 BOM 的 UTF-8 CSV |
  | `导出仪器工作量26年6-7月.xls` | .xls | Excel 导出的 HTML 表格 |
  | `导出科室工作量26年6-7月.xls` | .xls | Excel 导出的 HTML 表格 |
  | `25.9月生化LIS411 总汇表 result.xls` | .xls | 真 OLE/BIFF 工作簿 |

所以「扩展名 + magic」双重判断：magic 优先，扩展名只作为无法判定时的兜底。
这三种"假 .xls"必须能被读进来，否则用户只会看到一句"导入失败"。
"""
from __future__ import annotations

import os

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"   # 真 .xls（OLE 复合文档）
ZIP_MAGIC = b"PK"                                    # .xlsx/.xlsm/.xltx/.xltm

FMT_OLE_XLS = "ole_xls"
FMT_OPENXML = "openxml"
FMT_HTML = "html"
FMT_CSV = "csv"
FMT_TEXT = "text"
FMT_UNKNOWN = "unknown"

_OPENXML_EXTS = {".xlsx", ".xlsm", ".xltx", ".xltm"}
_TEXT_EXTS = {".csv", ".txt"}


def sniff_bytes(head: bytes) -> str:
    """按 magic 判断前若干字节的真实格式。"""
    if head.startswith(OLE_MAGIC):
        return FMT_OLE_XLS
    if head.startswith(ZIP_MAGIC):
        return FMT_OPENXML
    stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if stripped.startswith(b"<!doctype html") or stripped.startswith(b"<html") or stripped.startswith(b"<table"):
        return FMT_HTML
    # ★ 文本判定不能用「高位字节占比」——中文 CSV 天生就有 40%+ 的高位字节
    #   （实测 table_2.xls 占比 0.425），按阈值判会把正常中文表误杀成"未知"。
    #   改为「能不能严格解码成文本」，这才是文本与二进制的真实分界。
    # ★ 采样必须落在字符边界上：按固定 8192 字节切会把 UTF-8 多字节汉字劈开，
    #   解码直接抛 UnicodeDecodeError，又把合法 CSV 误判成二进制（实测踩过）。
    sample = head[:8192]
    sample = _trim_to_char_boundary(sample)
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            sample.decode(encoding)
        except UnicodeDecodeError:
            continue
        return FMT_TEXT
    return FMT_UNKNOWN


def _trim_to_char_boundary(chunk: bytes) -> bytes:
    """砍掉 UTF-8 末尾最多 3 字节的多字节残片，保证采样可在 UTF-8 下解码。

    GB18030 是双字节变长（最多 4 字节），这里按 4 字节上限回退，
    GB18030 失败时仍会被后面的 gb18030 解码分支接住。
    """
    for cut in range(4):
        end = len(chunk) - cut
        if end <= 0:
            return chunk
        try:
            chunk[:end].decode("utf-8")
            return chunk[:end]
        except UnicodeDecodeError:
            continue
    return chunk


def sniff_path(path: str) -> str:
    """判断一个文件的真实格式。

    ★ 内容判定优先，且**不按扩展名瞎猜**：`.xls` 内容其实是 CSV/HTML 是实测常态，
      猜成 BIFF 只会让 xlrd 抛 "Expected BOF record"，用户只看到一句"导入失败"。
      真的读不出内容时才认扩展名。
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            head = handle.read(8192)
    except OSError:
        head = b""
        size = 0
    if head:
        detected = sniff_bytes(head)
        if detected != FMT_UNKNOWN:
            return detected
        # ★ 读不出内容就返回 UNKNOWN，**不要**回退扩展名去"猜"一个格式：
        #   猜错会让后续分支抛出误导性的错误（如把乱码当"xls 已损坏"）。
        #   扩展名只用于判断"要不要接这个文件"，不能用来决定"怎么读"。
        return FMT_UNKNOWN
    return sniff_extension(os.path.splitext(path)[1]) if size == 0 else FMT_UNKNOWN


def sniff_extension(extension: str) -> str:
    ext = (extension or "").lower()
    if ext == ".xls":
        return FMT_OLE_XLS
    if ext in _OPENXML_EXTS:
        return FMT_OPENXML
    if ext in _TEXT_EXTS:
        return FMT_CSV
    return FMT_UNKNOWN


def is_excel_like(fmt: str) -> bool:
    return fmt in {FMT_OLE_XLS, FMT_OPENXML}


def describe_mismatch(extension: str, fmt: str) -> str:
    """给用户看得懂的原因说明（不要只丢一句"导入失败"）。"""
    ext = (extension or "").lower()
    if fmt == FMT_CSV and ext != ".csv":
        return "文件扩展名是 %s，但内容其实是 CSV 文本表；已按 CSV 方式读取。" % (ext or "未知")
    if fmt == FMT_HTML and ext != ".htm":
        return "文件扩展名是 %s，但内容其实是网页导出的表格（HTML）；已按表格方式读取。" % (ext or "未知")
    if is_excel_like(fmt) and ext not in _OPENXML_EXTS and ext != ".xls":
        return "文件扩展名是 %s，但内容是 Excel 工作簿（%s）；已按实际格式读取。" % (
            ext or "未知", "xls" if fmt == FMT_OLE_XLS else "xlsx",
        )
    return "已按实际内容格式（%s）读取。" % fmt


def accepts_extension(extension: str) -> bool:
    return (extension or "").lower() in (".xls", ".xlsx", ".xlsm", ".xltx", ".xltm", ".csv", ".txt")