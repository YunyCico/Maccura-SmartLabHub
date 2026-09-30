# -*- coding: utf-8 -*-
"""
智慧实验室数据汇总助手  ——  本地服务主程序
--------------------------------------------------
· 纯 Python 标准库 + openpyxl，无需安装 pandas / PySide6
· 所有数据都保存在本程序目录下，不联网（除非你启用 AI）
· 启动后自动在浏览器打开界面
"""

import os
import sys
import re
import io
import csv
import json
import base64
import shutil
import socket
import subprocess
import threading
import time
import uuid
import gzip
import webbrowser
import traceback
import unicodedata
from collections import Counter
import urllib.request
import urllib.error
from datetime import datetime, date, time as dtime
from difflib import SequenceMatcher
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote

APP_VERSION = "2.4.0"
# 表头/信息表识别规则的版本号。改动识别逻辑时把它 +1，
# 老索引会在下次启动时自动按新规则重算（用户不用重新导入数据）。
RECOGNIZE_VERSION = 4   # v4：丢弃"重复表头且整列无数据"的模板残留列（消除 _2 幽灵字段）

# ---------------------------------------------------------------- 基础路径
def _base_dir():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BASE_DIR = _base_dir()
DATA_DIR = os.path.join(BASE_DIR, "data", "smartlab")
LIB_DIR = os.path.join(DATA_DIR, "library")
SRC_DIR = os.path.join(LIB_DIR, "sources")
OUT_DIR = os.path.join(DATA_DIR, "exports")
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
def _index_file():
    return os.path.join(LIB_DIR, "index.json")
INDEX_FILE = _index_file()  # 兼容旧引用；实际读写走 _index_file()
RESULT_DIR = os.path.join(LIB_DIR, "results")
REPORT_DIR = os.path.join(DATA_DIR, "reports")
# v2.3.0「保存并替换原文件」的自动备份目录。
# ★ 放在 OUT_DIR 下面（而不是 library 里）是有意为之：library 是"数据源"的领地，
#   备份属于"历史产物"，混进去容易被后续的扫描/清理逻辑误伤。
BAK_DIR = os.path.join(OUT_DIR, "_原始文件备份")
MAX_RESULT_ROWS = 300000
PREVIEW_ROWS = 500          # 生成后首屏预览行数
MAX_PAGE_ROWS = 5000        # 单次翻页最多返回的行数
RAW_PREVIEW_MAX_COLS = 120  # 原始数据预览最多画多少列（防"拖格式"把列数撑到 16384 卡死浏览器）
PEEK_ROWS = 30             # 「表头结构分组 → 预览」回给前端看几行（够判断数据对不对）
KEEP_RESULTS = 20          # 结果在内存/磁盘里保留的份数

for d in (LIB_DIR, SRC_DIR, OUT_DIR, RESULT_DIR, REPORT_DIR):
    os.makedirs(d, exist_ok=True)

_lock = threading.RLock()

# ---------------------------------------------------------------- 工具函数
def norm(s):
    """字段名归一化：全角转半角、去空格标点、转小写"""
    if s is None:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    s = s.strip().lower()
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[、，,。.；;：:（）()\[\]【】{}<>《》/\\\-_—~!！?？“”\"'’‘*#·|＋+=]+", "", s)
    return s


_NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")
_DATE_RE = re.compile(r"^\d{4}[-/年]\d{1,2}([-/月]\d{1,2}日?)?$")


def is_num_text(t):
    return bool(_NUM_RE.match(str(t).strip().replace(",", "")))


def is_blank(v):
    if v is None:
        return True
    s = str(v).strip()
    return s == "" or s.lower() in ("none", "nan", "nat", "null", "#n/a")


def sim(a, b):
    """两个字段名的相似度 0~1"""
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    r = SequenceMatcher(None, a, b).ratio()
    if a in b or b in a:
        r = max(r, 0.82 + 0.18 * (min(len(a), len(b)) / max(len(a), len(b))))
    return round(r, 4)


def cell_to_text(v):
    """单元格值 -> 可读文本（None 返回空串）"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, datetime):
        if v.hour == 0 and v.minute == 0 and v.second == 0:
            return v.strftime("%Y-%m-%d")
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, date):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, dtime):
        return v.strftime("%H:%M:%S")
    if isinstance(v, float):
        if v == int(v) and abs(v) < 1e15:
            return str(int(v))
        return ("%.10f" % v).rstrip("0").rstrip(".")
    return str(v).strip()


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


DEFAULT_CONFIG = {
    "ai": {
        "enabled": False,
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "temperature": 0.2,
    },
    "labs": [],
    "table_types": [],
    "add_source_col": True,
    # 记录导入来源目录，方便"删错了"之后一键恢复
    "recent_source_dirs": [],
    # 字段映射记忆：{ "数据集ID||工作表": { 源列: 输出字段 } }
    # 用户手动调过的列映射会存下来，下次打开自动恢复
    "mapping": {},
    # 钉钉在线文档对接配置
    "dingtalk": {
        "enabled": False,
        "token": "",
        "port": 0,
        "doc_url": "",
        "cli_template": "",
        "download_dirs": [],
    },
}

STATE = {"version": 1, "datasets": [], "config": json.loads(json.dumps(DEFAULT_CONFIG))}


def load_state():
    global STATE
    if os.path.exists(_index_file()):
        try:
            with open(_index_file(), "r", encoding="utf-8") as f:
                data = json.load(f)
            cfg = json.loads(json.dumps(DEFAULT_CONFIG))
            cfg.update(data.get("config") or {})
            cfg["ai"] = dict(DEFAULT_CONFIG["ai"], **(data.get("config", {}).get("ai") or {}))
            STATE = {
                "version": data.get("version", 1),
                "datasets": data.get("datasets") or [],
                "config": cfg,
                # 必须保留：否则每次启动都以为识别规则变了，会全量重算一遍
                "recognize_version": data.get("recognize_version"),
            }
        except Exception:
            traceback.print_exc()
    _sweep_test_clones()
    return STATE


def _sweep_test_clones():
    """清掉上次运行残留的**测试副本**数据集（id 带 ds_clone_ 前缀）。

    为什么要在启动时扫一遍：自动化测试正常会自己 cleanup，但脚本中途崩了
    （或被 Ctrl-C）就会把副本留在索引里 —— 之后每次启动都多几条"测试副本"，
    甚至会干扰后续测试（比如"取第一个数据集"取到了副本）。启动时兜一次底，
    代价只有一次前缀比对。
    """
    try:
        clones = [d for d in STATE.get("datasets") or []
                  if str(d.get("id", "")).startswith(_CLONE_PREFIX)]
        if not clones:
            return
        for d in clones:
            pth = ds_abs_path(d)
            if pth and os.path.isfile(pth):
                try:
                    os.remove(pth)
                except OSError:
                    pass
            if pth and os.path.isdir(BAK_DIR):
                stem = os.path.splitext(os.path.basename(pth))[0]
                for f in list(os.listdir(BAK_DIR)):
                    if f.startswith(stem):
                        try:
                            os.remove(os.path.join(BAK_DIR, f))
                        except OSError:
                            pass
        STATE["datasets"] = [d for d in STATE["datasets"] if d not in clones]
        print("  [自检] 清理了 %d 个上次残留的测试副本" % len(clones))
    except Exception:
        traceback.print_exc()


def library_health():
    """
    自检：索引里的数据表，对应的原始文件是否都还在。

    为什么需要它 —— 以前的坑：程序目录被部分复制（只带了 index.json，
    没带 library/sources），或者有人手工清理了 sources 目录。
    这时索引里明明写着"有 6 张表"，界面上却什么都点不动，
    用户看到的是"6 张表 / 0 个字段"这种自相矛盾的状态，很难自己反应过来。

    这里把"缺文件"这件事显式报出来，前端就能直接提示怎么修。
    """
    missing = []
    for ds in STATE.get("datasets") or []:
        try:
            if not os.path.isfile(ds_abs_path(ds)):
                missing.append(ds.get("name") or ds.get("id") or "?")
        except Exception:
            missing.append(ds.get("name") or "?")
    return {
        "ok": not missing,
        "total": len(STATE.get("datasets") or []),
        "missing": missing,
        "src_dir": SRC_DIR,
    }


def save_state():
    with _lock:
        tmp = _index_file() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(STATE, f, ensure_ascii=False, indent=2)
        shutil.move(tmp, _index_file())


def get_config(public=True):
    cfg = json.loads(json.dumps(STATE["config"]))
    if public and cfg["ai"].get("api_key"):
        k = cfg["ai"]["api_key"]
        cfg["ai"]["api_key_masked"] = k[:4] + "*" * max(0, len(k) - 8) + k[-4:] if len(k) > 8 else "****"
        cfg["ai"]["has_key"] = True
    else:
        cfg["ai"]["has_key"] = bool(cfg["ai"].get("api_key"))
    # 钉钉 token 同样只回显掩码，不回传明文
    dt = cfg.setdefault("dingtalk", {})
    tk = dt.get("token") or ""
    dt["has_token"] = bool(tk)
    if tk:
        dt["token_masked"] = tk[:4] + "*" * max(0, len(tk) - 8) + tk[-4:] if len(tk) > 8 else "****"
    if public:
        dt["token"] = ""
    return cfg


# ---------------------------------------------------------------- 读取表格
_cache = {}
_raw_cache = {}
_merge_cache = {}
_CACHE_MAX = 24


def cached_raw(path, sheet_name):
    key = (path, sheet_name, os.path.getmtime(path) if os.path.exists(path) else 0)
    if key in _raw_cache:
        return _raw_cache[key]
    ext = os.path.splitext(path)[1].lower()
    merges = []
    if ext in (".xlsx", ".xlsm", ".xltx", ".xltm"):
        import openpyxl
        # 关键：先用正则扫出"真正带值的最大行"。
        # 否则遇到拖过格式的表（dimension 声称 1048576 行）会白读上百万行。
        real_max = max_data_row(path, sheet_name)
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            if sheet_name not in wb.sheetnames:
                raise ValueError("工作表不存在：%s" % sheet_name)
            ws = wb[sheet_name]
            rows = []
            limit = {}
            if real_max > 0:
                limit["max_row"] = real_max
            try:
                it = ws.iter_rows(values_only=True, **limit)
            except TypeError:
                it = ws.iter_rows(values_only=True)     # 老版本 openpyxl 不支持 max_row
            for r in it:
                rows.append(list(r))
        finally:
            try:
                wb.close()
            except Exception:
                pass
        merges = read_merges_from_xlsx(path, sheet_name)
    elif ext in (".csv", ".txt"):
        rows = read_csv_rows(path)
    else:
        raise ValueError("暂不支持的格式：%s（请先另存为 .xlsx）" % ext)

    n = max((len(r) for r in rows), default=0)
    for r in rows:
        if len(r) < n:
            r.extend([None] * (n - len(r)))
    if len(_raw_cache) >= _CACHE_MAX:
        for k in list(_raw_cache)[:len(_raw_cache) // 2]:
            _raw_cache.pop(k, None)
            _merge_cache.pop(k, None)
    _raw_cache[key] = rows
    _merge_cache[key] = merges
    return rows


def cached_merges(path, sheet_name):
    if (path, sheet_name, os.path.getmtime(path) if os.path.exists(path) else 0) not in _merge_cache:
        cached_raw(path, sheet_name)
    return _merge_cache.get((path, sheet_name, os.path.getmtime(path) if os.path.exists(path) else 0), [])


def _col_letters_to_idx(letters):
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch.upper()) - 64)
    return n - 1


def _xlsx_sheet_xml(path, sheet_name):
    """取出某个工作表的 sheetN.xml 文本（取不到返回 None）"""
    import zipfile
    import xml.etree.ElementTree as ET
    NS_M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    NS_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    try:
        z = zipfile.ZipFile(path)
    except Exception:
        return None
    with z:
        try:
            wbx = ET.fromstring(z.read("xl/workbook.xml"))
            rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        except Exception:
            return None
        relmap = {r.get("Id"): r.get("Target") for r in rels}
        target = None
        for sh in wbx.findall(".//%ssheets/%ssheet" % (NS_M, NS_M)):
            if sh.get("name") == sheet_name:
                target = relmap.get(sh.get(NS_R + "id"))
                break
        if not target:
            return None
        p = target.lstrip("/")
        if not p.startswith("xl/"):
            p = "xl/" + p.lstrip("./")
        try:
            return z.read(p).decode("utf-8", "replace")
        except KeyError:
            return None


# 匹配"带值"的单元格：<c r="B5" s="3"><v>1</v></c> 算，
# 只有样式没有值的 <c r="B5" s="3"/> 不算
_CELL_RE = re.compile(r'<c\s+r="[A-Z]+\d+"([^>]*?)(/?)>')


def max_data_row(path, sheet_name):
    """
    便宜地算出工作表里"真正带值的最大行号"。

    为什么需要：Excel 里给整列拖过边框/格式之后，dimension 会声称
    这个表有 1048576 行，XML 里也真的塞了上百万个 <row r="N"/> 空标签。
    如果直接信 openpyxl 的 max_row，就要白读上百万行 ——
    实测一个 4MB 的工作簿因此要 25 秒，内存暴涨。
    这里先用正则扫一遍，只取"带值的单元格"的最大行号，把读取量压到真实规模。
    """
    txt = _xlsx_sheet_xml(path, sheet_name)
    if not txt:
        return 0
    mx = 0
    for m in _CELL_RE.finditer(txt):
        attrs, selfclose = m.group(1), m.group(2)
        if selfclose:
            continue              # 空单元格（只有格式），不算数据
        r = m.start()
        # 从标签里把行号抠出来
        pm = re.search(r'r="[A-Z]+(\d+)"', m.group(0))
        if pm:
            v = int(pm.group(1))
            if v > mx:
                mx = v
    return mx


def read_merges_from_xlsx(path, sheet_name):
    """
    直接解析 xlsx 内部的 XML 取合并单元格区域（只读模式拿不到 merged_cells）。
    返回 [(起始行, 结束行, 起始列, 结束列)]；行从 1 开始编号，列从 0 开始。
    """
    out = []
    data = _xlsx_sheet_xml(path, sheet_name)
    if not data:
        return out
    for a, b in re.findall(r'<mergeCell[^>]*ref="([A-Z]+\d+):([A-Z]+\d+)"', data):
        mA = re.match(r"([A-Z]+)(\d+)", a)
        mB = re.match(r"([A-Z]+)(\d+)", b)
        if not mA or not mB:
            continue
        out.append((int(mA.group(2)), int(mB.group(2)),
                    _col_letters_to_idx(mA.group(1)), _col_letters_to_idx(mB.group(1))))
    return out


def read_csv_rows(path):
    raw = open(path, "rb").read()
    text = None
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk", "big5"):
        try:
            text = raw.decode(enc)
            break
        except Exception:
            continue
    if text is None:
        text = raw.decode("utf-8", "replace")
    delim = ","
    head = text[:2000]
    for cand in (",", "\t", ";", "|"):
        if head.count(cand) > head.count(delim):
            delim = cand
    rows = []
    for r in csv.reader(io.StringIO(text), delimiter=delim):
        rows.append(list(r))
    return rows


def parse_header_spec(spec):
    """'1' -> [1] ; '1-2' -> [1,2] ; '2,3' -> [2,3]"""
    spec = str(spec or "1").strip()
    out = []
    for part in re.split(r"[,，、\s]+", spec):
        if not part:
            continue
        m = re.match(r"^(\d+)\s*[-~至]\s*(\d+)$", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            out.extend(range(min(a, b), max(a, b) + 1))
        elif part.isdigit():
            out.append(int(part))
    out = sorted(set(x for x in out if x >= 1))
    return out or [1]


def build_header(rows, hdr_idx):
    """把多行表头合并成一行字段名"""
    ncol = max((len(r) for r in rows), default=0)
    names = []
    for c in range(ncol):
        parts = []
        for i in hdr_idx:
            if i < len(rows) and c < len(rows[i]):
                t = cell_to_text(rows[i][c])
                if t and (not parts or parts[-1] != t):
                    parts.append(t)
        names.append("-".join(parts))
    return names


def build_table(path, sheet_name, header_rows="1"):
    """返回 {columns:[], records:[[...]], rows:int}"""
    key = (path, sheet_name, str(header_rows))
    if key in _cache:
        return _cache[key]
    rows = cached_raw(path, sheet_name)
    hdr_idx = [i - 1 for i in parse_header_spec(header_rows)]
    hdr_idx = [i for i in hdr_idx if 0 <= i < len(rows)]
    if not hdr_idx:
        hdr_idx = [0]
    raw_names = build_header(rows, hdr_idx)
    start = max(hdr_idx) + 1

    # 列数：优先按表头行里出现的最大列，避免末尾空列把表撑得很宽
    hdr_width = max([len(rows[i]) for i in hdr_idx if i < len(rows)] or [0])
    ncol = max((len(r) for r in rows), default=0)

    # 表头原始行的"指纹"（用来跳过文件中间重复出现的表头行）
    hdr_sigs = set()
    for i in hdr_idx:
        if i < len(rows):
            sig = tuple(norm(cell_to_text(v)) for v in rows[i])
            if any(sig):
                hdr_sigs.add(sig)

    # ---- 关键改进：表头下方如果还有"标签在A列 / 内容在B列"的说明行，自动并入字段名 ----
    note_rows = []
    if hdr_width == 2:
        j = start
        blanks = 0
        while j < len(rows) and len(note_rows) < 20:
            r = rows[j]
            c0 = cell_to_text(r[0]) if len(r) > 0 else ""
            c1 = cell_to_text(r[1]) if len(r) > 1 else ""
            rest = [cell_to_text(v) for v in r[2:]]
            if not c0 and not c1 and not any(rest):
                blanks += 1
                if blanks >= 4:
                    break
                j += 1
                continue
            if c0 and not c1 and not any(rest) and not is_num_text(c0) and len(c0) <= 40:
                note_rows.append(j)
                blanks = 0
                j += 1
                continue
            break
    body_start = (note_rows[-1] + 1) if note_rows else start
    note_map = {}
    for j in note_rows:
        k = norm(cell_to_text(rows[j][0]))
        if k:
            note_map[k] = cell_to_text(rows[j][1]) if len(rows[j]) > 1 else ""

    body = []
    for r in rows[body_start:]:
        texts = [cell_to_text(v) for v in r]
        if not any(texts):
            continue
        if tuple(norm(t) for t in texts) in hdr_sigs:
            continue
        body.append(texts)

    # 表头名 + 同名说明行的内容（如"填写日期 | 2026-09-15"）
    enriched = []
    for nm in raw_names:
        extra = note_map.get(norm(nm))
        enriched.append("%s（%s）" % (nm, extra) if nm and extra else nm)

    # 去掉"表头为空 且 整列为空"的列
    keep = []
    for c in range(len(enriched)):
        if c >= ncol:
            break
        if enriched[c].strip():
            keep.append(c)
            continue
        if any(c < len(r) and r[c] != "" for r in body):
            keep.append(c)

    # 表头去重（同时保留一份"纯表头"的名字，供字段字典使用）
    # ★ 重复表头且整列无数据的列（模板残留空列）直接丢弃，不再产生 _2 幽灵字段；
    #   有数据的重复列仍保留并加 _2 后缀，绝不丢真数据。
    empty_cols = set()
    for c in range(ncol):
        if not any((r[c] if c < len(r) else "") not in ("", None) for r in body):
            empty_cols.add(c)
    final, base_final, seen, seen_base = [], [], {}, {}
    keep2 = []
    for c in keep:
        name = enriched[c].strip() or ("列%d" % (c + 1))
        bn = (raw_names[c].strip() if c < len(raw_names) else "") or ("列%d" % (c + 1))
        if (name in seen or bn in seen_base) and c in empty_cols:
            continue
        if name in seen:
            seen[name] += 1
            name = "%s_%d" % (name, seen[name])
        else:
            seen[name] = 1
        final.append(name)
        # 纯表头：不含程序为了提示拼上去的「（说明）」
        if bn in seen_base:
            seen_base[bn] += 1
            bn = "%s_%d" % (bn, seen_base[bn])
        else:
            seen_base[bn] = 1
        base_final.append(bn)
        keep2.append(c)

    columns = final
    base_columns = base_final
    keep = keep2
    records = [[(r[c] if c < len(r) else "") for c in keep] for r in body]

    # 说明行没有并进来的话，至少让用户能从表头看到后面还有内容
    # 注意：只能用于"窄表"（标签|值 形态）。宽表（比如 7 列的反馈明细）
    # 哪怕只有 1 行数据，那也是真数据，绝不能被当成说明行吃掉。
    if not note_rows and len(body) == 1 and hdr_width <= 2:
        tail = "；".join(t for t in body[0][:3] if t)
        if tail:
            columns = [c if i else "%s（%s）" % (c, tail[:40]) for i, c in enumerate(columns)]
            records = []

    res = {"columns": columns, "base_columns": base_columns,
           "records": records, "rows": len(records), "note_rows": note_rows,
           "keep": keep}
    _cache[key] = res
    return res


def _blank_run(rows, start, span):
    return all(all(is_blank(v) for v in rows[i]) for i in range(start, min(start + span, len(rows))))


# ---------------------------------------------------------------- 信息表（键值对）提取
def extract_info_map(rows, merges=None):
    """
    从"标签在左、内容在右"的信息表里把信息抠出来。
    例：  客户名称（全称） | 国药内蒙古一机医院
          院区 / 科室     | 检验科
          填报日期        | 2026-09-15
    只要某一行的 B 列有值、C 列以后为空，就认为是一条"键=值"。
    另外会自动识别"表格式区块"（横向表头 + 数据行），把数据行也带出来。

    返回 {"text": [(键, 值)], "blocks": [{"header": [...], "rows": [[...]]}]}
    """
    ncol = max((len(r) for r in rows), default=0)
    texts = [[cell_to_text(v) for v in r] for r in rows]
    used = set()
    kvs = []
    for i, t in enumerate(texts):
        nz = [(c, x) for c, x in enumerate(t) if x]
        if len(nz) == 2 and nz[0][0] == 0 and nz[1][0] == 1:
            k, v = nz[0][1], nz[1][1]
            if len(k) <= 40 and not is_num_text(k):
                kvs.append((k, v))
                used.add(i)

    blocks = []
    j = 0
    while j < len(rows):
        if j in used:
            j += 1
            continue
        # 找"横向表头"：本行非空且下一行也非空，且与下一行没有重复值
        if not any(texts[j]):
            j += 1
            continue
        nz_j = [(c, x) for c, x in enumerate(texts[j]) if x]
        if len(nz_j) >= 3:
            nxt = texts[j + 1] if j + 1 < len(texts) else []
            nz_n = [(c, x) for c, x in enumerate(nxt) if x]
            if len(nz_n) >= 3 and all(x[0] > 1 for x in nz_j[:3]) is False:
                hdr = [x for _, x in nz_j]
                hdr_cols = [c for c, _ in nz_j]
                if len(set(hdr)) == len(hdr) and not all(is_num_text(h) for h in hdr):
                    data = []
                    k = j + 1
                    while k < len(rows) and any(texts[k]) and k not in used:
                        if len([c for c, x in enumerate(texts[k]) if x]) < max(2, len(hdr_cols) // 2):
                            break
                        data.append([(texts[k][c] if c < len(texts[k]) else "") for c in hdr_cols])
                        used.add(k)
                        k += 1
                    if len(data) >= 2:
                        blocks.append({"header": hdr, "rows": data})
                        used.add(j)
                        j = k
                        continue
        j += 1
    return {"text": kvs, "blocks": blocks}


def build_info_table(path, sheet_name):
    """信息表 -> 可直接汇总的表现形式"""
    key = (path, sheet_name, "__info__")
    if key in _cache:
        return _cache[key]
    rows = cached_raw(path, sheet_name)
    info = extract_info_map(rows, cached_merges(path, sheet_name))

    # 文本信息表：转成"项目 | 内容"长表，方便和其它表叠起来
    records = [[k, v] for k, v in info["text"]]
    res = {"columns": ["项目", "内容"], "records": records, "rows": len(records)}

    # 表格式区块优先作为主表（有列结构，能跟别的表合并）
    for b in info["blocks"]:
        if len(b["header"]) >= 2 and len(b["rows"]) >= 3:
            res = {"columns": b["header"], "records": b["rows"], "rows": len(b["rows"]),
                   "info_kv": info["text"]}
            break
    _cache[key] = res
    return res


def _row_features(rows, i, look=400):
    """把第 i 行当作表头，看它下面 look 行能形成多"整齐"的表"""
    head = rows[i]
    ncol = len([v for v in head if not is_blank(v)])
    if ncol < 2:
        return None
    data = [r for r in rows[i + 1:i + 1 + look] if any(not is_blank(v) for v in r)]
    if len(data) < 3:
        return None
    filled = 0
    consistent = 0
    sample = data[:120]
    for c in range(min(len(head), 60)):
        if is_blank(head[c]):
            continue
        vals = [(r[c] if c < len(r) else None) for r in sample]
        nz = [v for v in vals if not is_blank(v)]
        if not nz:
            continue
        filled += 1
        ratio = len(nz) / max(1, len(sample))
        types = sum(1 for v in nz if not is_num_text(cell_to_text(v)))
        type_ratio = types / len(nz)
        if ratio >= 0.5 and (type_ratio >= 0.9 or type_ratio <= 0.1):
            consistent += 1
    if filled == 0:
        return None
    return {"row": i + 1, "ncol": ncol, "filled": filled, "consistent": consistent,
            "density": consistent / max(1, filled), "above_blank2": _blank_run(rows, max(0, i - 2), 2)}


def guess_header_row(rows, max_scan=25):
    """在前 max_scan 行里挑"最像表头"的一行"""
    cands = []
    for i in range(min(max_scan, max(0, len(rows) - 3))):
        f = _row_features(rows, i)
        if f:
            cands.append(f)
    if not cands:
        return 1
    best = max(cands, key=lambda f: (f["consistent"], f["filled"], f["above_blank2"], -f["row"]))
    # 上方紧邻的就是标题/说明行（表头上方有空格）→ 保持不动
    if best["above_blank2"]:
        return best["row"]
    # 上面还有一行"文字多、列齐"的兄弟行，且当前行下方有较多空行 → 那更像说明区，取上一行
    prev = next((f for f in cands if f["row"] == best["row"] - 1), None)
    if prev and prev["consistent"] >= best["consistent"] and prev["density"] >= 0.9:
        return prev["row"]
    return best["row"]


def _covered_by_merge(col, row, merges):
    """该单元格是否落在某个横向合并区域里（说明它是被父表头覆盖的子表头）"""
    for (r1, r2, c1, c2) in merges or []:
        if r1 <= row <= r2 and c1 <= col <= c2 and c1 != c2:
            return True
    return False


def _ident_count(nz_vals):
    vals = [v for _, v in nz_vals]
    if not vals:
        return 0
    the_most = max(set(vals), key=vals.count)
    spread = len(set(vals)) / len(vals)
    return vals.count(the_most) if spread < 0.5 else 0


def _header_run(rows, start_idx):
    """从 start_idx 行往下数，最多连续几行能当"表头区"（长度 1~3）"""
    best = 1
    for k in (2, 3):
        i = start_idx + k - 1
        if i >= len(rows):
            break
        if _header_region_ok(rows, start_idx, i):
            best = k
        else:
            break
    return best


def _looks_data(rows, start_idx, span):
    """表头区下面是不是真的跟着数据行"""
    i = start_idx + span
    if i >= len(rows):
        return False
    head = [cell_to_text(v) for v in rows[start_idx]]
    for k in range(i, min(i + 3, len(rows))):
        r = rows[k]
        nz = [(c, cell_to_text(v)) for c, v in enumerate(r) if cell_to_text(v)]
        if len(nz) < 2:
            continue
        # 大部分格子有值、且与表头行不重合
        if len(nz) >= max(2, int(len([t for t in head if t]) * 0.5)):
            return True
    return False


def detect_header_block(rows, merges=None, fallback=1):
    """
    判断表头占了哪几行，返回 '1' / '1-2' / '16' 这样的写法。
    典型的多行表头：
        第1行  序号 | 医院 | 提交日期 | 处理情况(跨两列合并)
        第2行                          | 响应次数 | 处理状态
    fallback：识别不出可信表头时退回的行号（导入时传"上一版识别结果"，避免越改越差）
    """
    best_start, best_span, best_score = None, 1, -1
    best_thin = False
    # ★ 空表（0 行）直接退出，别把 range 的 max(1, ...) 当成"至少试第 0 行"：
    #   `min(25, max(1, len(rows) - 1))` 在 len(rows)==0 时会返回 1，
    #   于是下面 `rows[0]` 抛 IndexError。这个入口以前只有"导入"会用，
    #   而导入的文件一定有行；v2.3.0 起"清空整表后保存"也会走到这里
    #   （用户把一张表清空了 → 回写 → 重解析 → 0 行），就踩到了。
    if not rows:
        return str(fallback or 1)
    for a in range(0, min(25, max(1, len(rows) - 1))):
        head = [cell_to_text(v) for v in rows[a]]
        nz = [(i, t) for i, t in enumerate(head) if t]
        if len(nz) < 2:
            continue
        # 真表头上面通常是标题/空行（窄），而数据行上面紧挨着一条"同样宽的满行"。
        # 例：第1行是表头，第2行起是数据 —— 单看第2行，各列类型非常整齐，
        # 得分反而比第1行还高，很容易把第一条数据行当成表头（字段名变成 列6/列7/列8）。
        prev_full = False
        if a > 0:
            prev_nz = [t for t in (cell_to_text(v) for v in rows[a - 1]) if t]
            if len(prev_nz) >= len(nz) and len(prev_nz) >= 3:
                prev_full = True
        # 试 span = 1 / 2 / 3
        for span in (1, 2, 3):
            if a + span >= len(rows):
                break
            if span > 1:
                ok = True
                for k in range(a + 1, a + span):
                    vals = [cell_to_text(v) for v in rows[k]]
                    nzk = [(i, t) for i, t in enumerate(vals) if t]
                    if not nzk or any(is_num_text(t) for _, t in nzk):
                        ok = False
                        break
                    # 子表头必须能解释：正上方为空 或 落在横向合并区
                    if not all(_covered_by_merge(i, k, merges) or i >= len(head) or not head[i]
                               for i, _ in nzk):
                        ok = False
                        break
                if not ok:
                    break

            # 表头行数 = span，下行开始数数据行
            data = [r for r in rows[a + span:] if sum(1 for v in r if not is_blank(v)) >= 2]
            if not data:
                continue
            # 数据行不足 3 行时，"数据列类型是否稳定"这套统计判据失效。
            # 小表（上面标题/说明、中间真表头、下面只有一两行数据）改成强判据：
            # 表头必须至少有 3 个非空单元格，且不能混有数字。
            thin = len(data) < 3
            head_num_pre = sum(1 for _, t in nz if is_num_text(t))
            if thin and (len(nz) < 3 or head_num_pre > 0):
                continue
            # 关键：数据行的每一列要有稳定的"类型"（数字列/文本列），否则说明认错表头了。
            # 注意：稀疏列（填得很少，比如整列还没填）不能算作"不整齐"，
            # 否则一份"下游还没填"的空模板会被判成没表头，字段名就变成 列6/列7/列8。
            sample = data[:150]
            dense = consistent = 0
            for c in range(min(len(head), 60)):
                if not head[c]:
                    continue
                vals = [cell_to_text(r[c]) if c < len(r) else "" for r in sample]
                nzvals = [v for v in vals if v]
                if not nzvals:
                    continue
                if len(nzvals) / len(sample) < 0.4:
                    continue          # 稀疏列：多半是"还没填"，不参与整齐度打分
                dense += 1            # 只有"填得比较满"的列才当分母
                nums = sum(1 for v in nzvals if is_num_text(v))
                ratio = nums / len(nzvals)
                if ratio >= 0.9 or ratio <= 0.1:
                    consistent += 1
            if dense == 0:
                continue
            density = consistent / dense
            # 表头本身要是"标签"，不能大量是数字
            head_num = sum(1 for _, t in nz if is_num_text(t))
            # 多行表头（父表头+子表头）能解释更多列信息，优先采用
            score = density * 100 + consistent * 2 - head_num * 6 + (8 if span > 1 else 0) - a * 0.15
            if density < 0.75:
                score -= 40
            # 这一行的这一列，下面全是空的（可能只是"侧边说明文字"，不是真表头）。
            # 注意：真表头里也常有"还没填的列"（例如"是否重点关注"整列空着），
            # 所以只有"多数列都空"才算侧边说明，不能因为个别空列就把真表头否掉
            # ——否则会把第一条数据行当成表头，字段名就变成 列6/列7/列8 这种垃圾。
            below_blank = 0
            for c in [_c for _c, _t in nz]:
                if not any((cell_to_text(r[c]) if c < len(r) else "") for r in sample[:150]):
                    below_blank += 1
            if below_blank >= 2 and below_blank / max(1, len(nz)) >= 0.5:
                score -= below_blank * 12
            # 正上方已经是同样宽的满行 → 这是数据行，不是表头
            if prev_full:
                score -= 25
            if thin:
                # 小表容错：数据行不足以做统计判据时，用上面的强判据兜底。
                # 但如果这一行正上方已经是满行，那它明显是数据行，不能给这个加分，
                # 否则表尾那几行会反过来压过真表头（表头被认到第 9 行去）。
                thin_ok = not prev_full
            else:
                thin_ok = False
            if thin_ok:
                score += 50
            if score > best_score:
                best_score, best_start, best_span = score, a + 1, span
                best_thin = thin_ok

    if best_start is None:
        return str(fallback)
    # 小表里已经用强判据确认过的候选，就不要再被 60 分门槛否掉
    if best_score < 60 and not best_thin:
        # 没有可信候选：退回上一版结果（首次导入时用简单启发式）
        if fallback:
            return str(fallback)
        return str(guess_header_row(rows))
    return "%d" % best_start if best_span == 1 else "%d-%d" % (best_start, best_start + best_span - 1)


def sheet_names(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm", ".xltx", ".xltm"):
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            return list(wb.sheetnames)
        finally:
            try:
                wb.close()
            except Exception:
                pass
    return ["CSV"]


# ---------------------------------------------------------------- 自动打标签
def guess_lab(name, labs):
    stem = os.path.splitext(os.path.basename(name))[0]
    for lab in labs:
        if lab and (lab in stem or norm(lab) in norm(stem)):
            return lab
    # 注意：\u3007 是「〇」（如"九〇三医院"），它不在 \u4e00-\u9fa5 里，
    # 漏掉它会导致"九〇三医院"识别不出实验室名。
    cjk = r"[\u4e00-\u9fa5\u3007A-Za-z0-9]"
    # 优先取"完整机构名"（到"医院"为止），否则"漯河市中心医院"会被截成"漯河市"
    m = re.match(r"^(" + cjk + r"{2,20}?(?:人民医院|中心医院|中医医院|蒙医中医医院|"
                 r"妇幼保健院|卫生院|医院))", stem)
    if m:
        return m.group(1)
    m = re.search(r"(" + cjk + r"{2,20}?)(?:智慧实验室|实验室)", stem)
    if m:
        return m.group(1)
    m = re.match(r"^(" + cjk + r"+?)(?:市|县|区|医院|人民医院)", stem)
    if m:
        return m.group(0)
    return ""


def guess_table_type(name):
    stem = os.path.splitext(os.path.basename(name))[0]
    for kw in ("问题、需求反馈表", "需求反馈表", "问题反馈表", "测试数分析表", "数据分析表",
               "满意度", "服务需求", "问题清单", "需求清单", "月报", "季报", "台账", "汇总表"):
        if kw in stem:
            return kw
    m = re.search(r"(?:智慧实验室|实验室)(.{2,20}?)(?:表|\d{4})?$", stem)
    if m:
        return m.group(1)
    return ""


def ds_summary(ds):
    total_rows = sum(s.get("rows", 0) for s in ds.get("sheets", []))
    return {
        "id": ds["id"],
        "name": ds["name"],
        "lab": ds.get("lab", ""),
        "table_type": ds.get("table_type", ""),
        "note": ds.get("note", ""),
        "stored": ds["stored"],
        "src_path": ds.get("src_path", ""),
        "size": ds.get("size", 0),
        "imported_at": ds.get("imported_at", ""),
        "sheet_count": len(ds.get("sheets", [])),
        "field_count": max([len(s.get("columns", [])) for s in ds.get("sheets", [])] or [0]),
        "total_rows": total_rows,
        "sheets": ds.get("sheets", []),
    }


# ---------------------------------------------------------------- 导入
def ensure_sheet_meta(ds, stored_abs, sn, rows):
    """（重）解析单个工作表的结构信息"""
    hr = detect_header_block(rows, cached_merges(stored_abs, sn))
    try:
        t = build_table(stored_abs, sn, hr)
    except Exception as e:
        return {"name": sn, "kind": "table", "header_rows": hr, "rows": 0,
                "columns": [], "signature": "", "error": str(e)}
    kind = "table"
    if t["rows"] <= 1:
        info = extract_info_map(rows, cached_merges(stored_abs, sn))
        if len(info["text"]) >= 3:
            kind = "info"
            t = build_info_table(stored_abs, sn)
    return {
        "name": sn,
        "kind": kind,
        "header_rows": hr,
        "rows": t["rows"],
        "columns": t["columns"],
        "base_columns": t.get("base_columns") or t["columns"],
        "signature": "|".join(sorted(norm(c) for c in t["columns"] if norm(c))),
        "sample": t["records"][:3],
    }


def refresh_metadata(force=False):
    """
    老索引升级：用新版表头识别重新解析所有工作表。
    只更新结构信息，不动数据本身，用户不用重新导入。
    """
    changed = 0
    # 识别规则升级过（或用户点了强制刷新）→ 所有数据集都要重算
    need_all = force or STATE.get("recognize_version") != RECOGNIZE_VERSION
    if need_all:
        _cache.clear()          # 规则变了，旧缓存必须作废，否则还是会用老结果
    for ds in STATE["datasets"]:
        p = ds_abs_path(ds)
        if not os.path.isfile(p):
            continue
        try:
            names = sheet_names(p)
        except Exception:
            continue
        # ★ 源文件被外部改写 / 从备份恢复时（mtime 或大小变化）也要重解析，
        #   否则索引里还是旧的列结构。
        try:
            st = os.stat(p)
            fp_changed = (ds.get("src_mtime") != st.st_mtime) or (ds.get("src_size") != st.st_size)
            ds["src_mtime"] = st.st_mtime
            ds["src_size"] = st.st_size
        except OSError:
            fp_changed = False
        if not need_all:
            same_names = [s["name"] for s in ds.get("sheets", [])] == names
            has_kind = all(s.get("kind") for s in ds.get("sheets", []))
            if same_names and has_kind and not fp_changed:
                continue
        old = {s["name"]: s for s in ds.get("sheets", [])}
        new = []
        for sn in names:
            try:
                rows = cached_raw(p, sn)
            except Exception as e:
                new.append(dict(old.get(sn) or {"name": sn, "rows": 0, "columns": []}, error=str(e)))
                continue
            meta = ensure_sheet_meta(ds, p, sn, rows)
            prev = old.get(sn) or {}
            # 用户手动改过表头行的，保留用户设置
            if prev.get("user_header") and prev.get("header_rows"):
                try:
                    meta["header_rows"] = prev["header_rows"]
                    t = (build_info_table(p, sn) if prev.get("kind") == "info"
                         else build_table(p, sn, prev["header_rows"]))
                    meta["columns"] = t["columns"]
                    meta["base_columns"] = t.get("base_columns") or t["columns"]
                    meta["rows"] = t["rows"]
                    meta["sample"] = t["records"][:3]
                    meta["signature"] = "|".join(sorted(norm(c) for c in t["columns"] if norm(c)))
                except Exception as e:
                    meta["error"] = str(e)
            for k in ("lab", "note", "enabled", "user_header"):
                if k in prev:
                    meta[k] = prev[k]
            if prev.get("signature") != meta.get("signature") or prev.get("rows") != meta.get("rows"):
                changed += 1
            new.append(meta)
        ds["sheets"] = new
        if not ds.get("lab"):
            ds["lab"] = guess_lab(ds["name"], STATE["config"].get("labs") or [])
        if not ds.get("table_type"):
            ds["table_type"] = guess_table_type(ds["name"])
    STATE["recognize_version"] = RECOGNIZE_VERSION
    return changed


def import_file(path, copy=True, lab="", table_type="", header_rows=None, display_name=None):
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise ValueError("文件不存在：%s" % path)
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".xlsx", ".xlsm", ".xltx", ".xltm", ".csv", ".txt"):
        raise ValueError("暂不支持的格式：%s（.xls 请用 Excel 另存为 .xlsx）" % ext)

    show_name = display_name or os.path.basename(path)
    size = os.path.getsize(path)
    for old in STATE["datasets"]:
        if old.get("name") == show_name and old.get("size") == size:
            raise ValueError("已导入过同名同大小的文件，跳过（如需重新导入请先删除它）")

    ds_id = "ds_" + uuid.uuid4().hex[:10]
    stored_name = ds_id + "_" + re.sub(r'[\\/:*?"<>|]', "_", show_name)
    stored_rel = os.path.join("sources", stored_name)
    stored_abs = os.path.join(LIB_DIR, stored_rel)
    shutil.copy2(path, stored_abs)

    sheets_meta = []
    for sn in sheet_names(stored_abs):
        try:
            rows = cached_raw(stored_abs, sn)
        except Exception as e:
            sheets_meta.append({"name": sn, "header_rows": "1", "rows": 0,
                                "columns": [], "signature": "", "error": str(e)})
            continue
        if header_rows:
            hr = str(header_rows)
        else:
            hr = detect_header_block(rows, cached_merges(stored_abs, sn))
        try:
            t = build_table(stored_abs, sn, hr)
        except Exception as e:
            sheets_meta.append({"name": sn, "header_rows": hr, "rows": 0,
                                "columns": [], "signature": "", "error": str(e)})
            continue
        kind = "table"
        # 列式解析几乎没结果，且"标签|值"成对出现 → 判定为信息表
        if t["rows"] <= 1:
            info = extract_info_map(rows, cached_merges(stored_abs, sn))
            if len(info["text"]) >= 3:
                kind = "info"
                it = build_info_table(stored_abs, sn)
                t = it
        sheets_meta.append({
            "name": sn,
            "kind": kind,
            "header_rows": hr,
            "rows": t["rows"],
            "columns": t["columns"],
            "base_columns": t.get("base_columns") or t["columns"],
            "signature": "|".join(sorted(norm(c) for c in t["columns"] if norm(c))),
            "sample": t["records"][:3],
        })

    cfg = STATE["config"]
    ds = {
        "id": ds_id,
        "name": show_name,
        "stored": stored_rel.replace("\\", "/"),
        # 记住它当初是从哪儿导进来的。删掉记录后想找回数据时，
        # 靠这个能把原文件重新捞回来（原件不会因为删记录而被删）。
        "src_path": os.path.abspath(path),
        "size": size,
        "imported_at": now_str(),
        "lab": lab or guess_lab(show_name, cfg.get("labs") or []),
        "table_type": table_type or guess_table_type(show_name),
        "note": "",
        "sheets": sheets_meta,
    }
    STATE["datasets"].append(ds)
    # 记录导入来源目录，方便"一键恢复"
    try:
        d = os.path.dirname(os.path.abspath(path))
        arr = cfg.setdefault("recent_source_dirs", [])
        if d and d not in arr:
            arr.insert(0, d)
        cfg["recent_source_dirs"] = arr[:12]
    except Exception:
        pass
    return ds


def ds_abs_path(ds):
    return os.path.join(LIB_DIR, ds["stored"].replace("/", os.sep))


def reparse_dataset(ds):
    """重新解析一个**已存在**的数据集，把 ds["sheets"] / size 刷新成文件的当前状态。

    为什么需要它 —— v2.3.0 起了「保存并替换原文件」：编辑结果会真的写回
    library/sources 里的那份文件。写完之后索引（工作表列表、表头识别、列名、
    行数）就过期了：
      · 删掉的子表还挂在 ds["sheets"] 里，界面点得动但文件里根本没有；
      · 改过/删过列的表，列名与列数对不上，汇总时会漏列或报错。
    所以回写必须紧跟一次重解析。

    为什么不能直接复用 import_file —— 它会**再复制一份文件**、生成新 id、
    并且拿 `name + size` 去重（回写后 size 变了反而不触发，但复制是白费的）。
    这里只做"就地刷新元数据"。

    保留用户手动设置：header_rows（user_header）、lab、note、enabled。
    """
    p = ds_abs_path(ds)
    if not os.path.isfile(p):
        return 0
    old = {s["name"]: s for s in ds.get("sheets", [])}
    new = []
    for sn in sheet_names(p):
        try:
            rows = cached_raw(p, sn)
        except Exception as e:
            new.append(dict(old.get(sn) or {"name": sn, "rows": 0, "columns": []}, error=str(e)))
            continue
        meta = ensure_sheet_meta(ds, p, sn, rows)
        prev = old.get(sn) or {}
        # 用户手动指定过表头行的，尊重用户设置（他可能就是想让程序别乱猜）
        if prev.get("user_header") and prev.get("header_rows"):
            try:
                meta["header_rows"] = prev["header_rows"]
                t = (build_info_table(p, sn) if prev.get("kind") == "info"
                     else build_table(p, sn, prev["header_rows"]))
                meta["columns"] = t["columns"]
                meta["base_columns"] = t.get("base_columns") or t["columns"]
                meta["rows"] = t["rows"]
                meta["sample"] = t["records"][:3]
                meta["signature"] = "|".join(sorted(norm(c) for c in t["columns"] if norm(c)))
            except Exception as e:
                meta["error"] = str(e)
        for k in ("lab", "note", "enabled", "user_header"):
            if k in prev:
                meta[k] = prev[k]
        new.append(meta)
    ds["sheets"] = new
    try:
        ds["size"] = os.path.getsize(p)
    except OSError:
        pass
    return len(new)


# ---------------------------------------------------------------- 字段字典
def field_dictionary():
    """
    字段字典。
    · 字段名一律只取"表头本身"：程序为了提示而拼上去的「（说明）」不算字段名，
      它进"同义写法/完整名"里，不参与归类。
    · 同时给出每张数据表里有没有这个字段（sheet_keys），
      前端据此画"横向矩阵"（字段 × 数据表）以及判断能不能纵向合并。
    """
    dic = {}
    for ds in STATE["datasets"]:
        for sh in ds.get("sheets", []):
            cols = sh.get("columns", []) or []
            bases = sh.get("base_columns") or []
            sheet_key = "%s||%s" % (ds["id"], sh["name"])
            for i, c in enumerate(cols):
                disp = (bases[i] if i < len(bases) else c) or c
                disp = str(disp).strip()
                k = norm(disp)
                if not k:
                    continue
                e = dic.setdefault(k, {
                    "key": k, "names": {}, "datasets": {}, "sheets": {},
                    "fulls": set(), "rows": 0,
                })
                e["names"][disp] = e["names"].get(disp, 0) + 1
                if str(c) != disp:
                    e["fulls"].add(str(c))
                e["datasets"][ds["id"]] = True
                e["sheets"][sheet_key] = True
                e["rows"] += sh.get("rows", 0) or 0

    out = []
    for k, e in dic.items():
        names = sorted(e["names"].items(), key=lambda x: (-x[1], x[0]))
        out.append({
            "key": k,
            "name": names[0][0],
            "aliases": [n for n, _ in names[1:]],
            "full_names": sorted(e["fulls"])[:6],
            "dataset_count": len(e["datasets"]),
            "dataset_ids": list(e["datasets"].keys()),
            "sheet_count": len(e["sheets"]),
            "sheet_keys": list(e["sheets"].keys()),
            "rows": e["rows"],
        })
    out.sort(key=lambda x: (-x["dataset_count"], -x["sheet_count"], x["name"]))
    return out


def delete_fields(keys, dataset_ids=None):
    """
    从字段字典删除字段，并同步删除原始表里对应的数据：
    · 普通表（table）：删除该字段对应的整列；
    · 信息表（info，标签|值 结构）：删除该字段对应的整行。
    走 raw_edit 覆盖层 + 回写原文件（自动备份），再重解析，字典随之更新。
    dataset_ids 传入时只处理这些数据集（用于隔离测试 / 定向清理）。
    """
    keys = set(norm(k) for k in (keys or []) if k)
    if not keys:
        raise ValueError("没有要删除的字段")
    scope = set(dataset_ids) if dataset_ids else None
    touched = []
    errors = []
    for ds in STATE["datasets"]:
        if scope and ds["id"] not in scope:
            continue
        pth = ds_abs_path(ds)
        for sh in ds.get("sheets", []):
            name = sh.get("name")
            kind = sh.get("kind") or "table"
            try:
                rows = cached_raw(pth, name)
            except Exception as ex:
                errors.append("%s / %s：读取失败 %s" % (ds.get("name"), name, ex))
                continue
            if not rows:
                continue
            ops = []
            if kind == "info":
                for r, row in enumerate(rows):
                    lab = norm(cell_to_text(row[0]) if row else "")
                    if lab in keys:
                        ops.append({"t": "row", "r": r})
            else:
                hr = max(1, int(sh.get("header_rows") or 1))
                try:
                    t = build_table(pth, name, hr)
                except Exception as ex:
                    # ★ 不再静默跳过：解析失败要回传给界面，避免"点了没反应"
                    traceback.print_exc()
                    errors.append("%s / %s：解析失败 %s" % (ds.get("name"), name, ex))
                    continue
                keep = t.get("keep") or []
                base = t.get("base_columns") or []
                for i, bn in enumerate(base):
                    if i >= len(keep):
                        break
                    if norm(str(bn)) in keys:
                        ops.append({"t": "col", "c": keep[i]})
            if not ops:
                continue
            try:
                raw_edit_apply(ds["id"], name, ops)
                raw_edit_save(ds["id"], name, backup=True)
                reparse_dataset(ds)
                touched.append("%s / %s" % (ds.get("name"), name))
            except Exception as ex:
                traceback.print_exc()
                errors.append("%s / %s：写回失败 %s" % (ds.get("name"), name, ex))
    save_state()
    return {"ok": True, "deleted": sorted(keys), "touched": touched, "errors": errors}


# ---------------------------------------------------------------- 汇总引擎
def apply_filters(rows, cols, filters):
    idx = {c: i for i, c in enumerate(cols)}
    out = rows
    for f in filters or []:
        field = f.get("field")
        op = (f.get("op") or "contains").lower()
        val = f.get("value", "")
        if field not in idx:
            continue
        i = idx[field]

        def ok(r):
            v = r[i] if i < len(r) else ""
            s = str(v)
            if op == "empty":
                return s.strip() == ""
            if op == "notempty":
                return s.strip() != ""
            if op == "eq":
                return norm(s) == norm(val)
            if op == "ne":
                return norm(s) != norm(val)
            if op == "contains":
                return norm(val) in norm(s)
            if op == "notcontains":
                return norm(val) not in norm(s)
            try:
                a = float(s)
                b = float(val)
            except Exception:
                return False
            if op == "gt":
                return a > b
            if op == "lt":
                return a < b
            if op == "ge":
                return a >= b
            if op == "le":
                return a <= b
            return True

        out = [r for r in out if ok(r)]
    return out


def source_table(ds, sheet_name, header_rows, kind="auto"):
    p = ds_abs_path(ds)
    sh = next((s for s in ds["sheets"] if s["name"] == sheet_name), None)
    if sh is None:
        raise ValueError("工作表不存在：%s" % sheet_name)
    hr = str(header_rows or sh.get("header_rows") or "1")
    # 信息表模式：按"标签|内容"提取，或自动判断为信息表
    if kind == "info":
        return build_info_table(p, sheet_name)
    if kind == "auto":
        # 手改过表头行的一律按用户设置走；否则看看像不像信息表
        if str(sh.get("header_rows")) == "info" or sh.get("kind") == "info":
            return build_info_table(p, sheet_name)
    t = build_table(p, sheet_name, hr)
    if kind == "auto" and t["rows"] <= 1:
        # 列式解析几乎没结果，且表里"标签|值"成对出现 → 按信息表处理
        rows = cached_raw(p, sheet_name)
        info = extract_info_map(rows, cached_merges(p, sheet_name))
        if len(info["text"]) >= 3:
            it = build_info_table(p, sheet_name)
            it["auto_info"] = True
            return it
    return t


def auto_mapping(src_cols, target_cols):
    """源列 -> 目标列 自动映射"""
    mapping, used = {}, set()
    for sc in src_cols:
        best, best_score = "", 0.0
        for tc in target_cols:
            if tc in used:
                continue
            s = sim(sc, tc)
            if s > best_score:
                best, best_score = tc, s
        if best and best_score >= 0.86:
            mapping[sc] = best
            used.add(best)
    return mapping


def aggregate(req):
    mode = req.get("mode") or "union"
    fields = [f for f in (req.get("fields") or []) if f]
    filters = req.get("filters") or []
    add_src = bool(req.get("add_source_col", True))
    dedup = bool(req.get("dedup", False))
    sort = req.get("sort") or {}
    limit = int(req.get("limit") or 0)
    by_id = {d["id"]: d for d in STATE["datasets"]}

    # 关键前置检查：源文件是否还在磁盘上。
    # library/sources 被清空 / 换电脑 / 拷贝程序时只带了 index.json，
    # 都会导致"点了没反应"。这里必须明确告诉用户，而不是默默返回 0 行。
    missing = []
    for s in req.get("sources") or []:
        ds = by_id.get(s.get("dataset_id"))
        if not ds:
            continue
        if not os.path.isfile(ds_abs_path(ds)):
            if ds["name"] not in missing:
                missing.append(ds["name"])
    if missing:
        raise ValueError(
            "找不到源文件，无法汇总。以下 %d 个数据表的原始文件已不在程序里：\n\n"
            "%s\n\n"
            "常见原因：① 拷贝程序时只拷了 index.json，没拷 library/sources 文件夹；"
            "② 手工清理过 library/sources；③ 换了一台电脑。\n\n"
            "解决办法：到「数据源管理」页，把这些表名的旧记录删掉，然后重新导入原始 Excel 文件即可（导入后字段会自动识别）。"
            % (len(missing), "\n".join("  · " + m for m in missing[:12]))
        )

    if mode == "join":
        result = aggregate_join(req, by_id, filters, add_src, dedup, sort, limit)
    else:
        result = aggregate_union(req, by_id, fields, filters, add_src, dedup, sort, limit)
    return result


def aggregate_union(req, by_id, fields, filters, add_src, dedup, sort, limit):
    out_fields = list(fields)
    if add_src:
        for extra in ("来源实验室", "来源文件", "来源工作表"):
            if extra in out_fields:
                out_fields.remove(extra)
        out_fields = ["来源实验室", "来源文件", "来源工作表"] + out_fields

    rows = []
    detail, skipped = [], []
    for s in req.get("sources") or []:
        ds = by_id.get(s.get("dataset_id"))
        if not ds:
            continue
        try:
            t = source_table(ds, s.get("sheet"), s.get("header_rows"), s.get("kind") or "auto")
            if t.get("auto_info"):
                skipped.append({"dataset": ds["name"], "sheet": s.get("sheet"),
                                "reason": "这张表是信息表（标签/内容成对），已按信息表读取"})
        except Exception as e:
            skipped.append({"dataset": ds["name"], "sheet": s.get("sheet"), "reason": str(e)})
            continue
        mapping = s.get("mapping") or {}
        if not mapping:
            mapping = auto_mapping(t["columns"], fields)
        pos = {c: i for i, c in enumerate(t["columns"])}
        sub = apply_filters(t["records"], t["columns"], filters)
        detail.append({
            "dataset_id": ds["id"], "dataset": ds["name"], "lab": ds.get("lab", ""),
            "sheet": s.get("sheet"), "used_rows": len(sub), "total_rows": t["rows"],
            "columns": t["columns"], "mapping": mapping,
        })
        for r in sub:
            o = {}
            for sc, tc in mapping.items():
                if tc in out_fields and sc in pos and pos[sc] < len(r):
                    v = r[pos[sc]]
                    if v != "" or not tc in o:
                        o[tc] = v
            if add_src:
                o["来源实验室"] = ds.get("lab", "") or "-"
                o["来源文件"] = ds["name"]
                o["来源工作表"] = s.get("sheet")
            rows.append([o.get(f, "") for f in out_fields])

    if dedup:
        seen, uniq = set(), []
        for r in rows:
            k = tuple(r)
            if k not in seen:
                seen.add(k)
                uniq.append(r)
        rows = uniq

    if sort.get("field") and sort["field"] in out_fields:
        i = out_fields.index(sort["field"])
        rows.sort(key=lambda r: str(r[i] if i < len(r) else ""), reverse=bool(sort.get("desc")))
    elif mode_sort_default(req):
        pass

    truncated = False
    if limit and len(rows) > limit:
        rows = rows[:limit]
        truncated = True

    return {"columns": out_fields, "rows": rows, "detail": detail, "skipped": skipped,
            "truncated": truncated, "mode": "union"}


def mode_sort_default(req):
    return False


def aggregate_join(req, by_id, filters, add_src, dedup, sort, limit):
    base = req.get("base") or {}
    ds = by_id.get(base.get("dataset_id"))
    if not ds:
        raise ValueError("未指定主表")
    bt = source_table(ds, base.get("sheet"), base.get("header_rows"), base.get("kind") or "auto")
    base_rows = apply_filters(bt["records"], bt["columns"], filters) if base.get("filter_on_base") else bt["records"]

    on_base = base.get("on") or ""
    if on_base not in bt["columns"]:
        raise ValueError("主表上找不到关联字段：%s" % on_base)

    out_cols = list(bt["columns"])
    key_vals = []   # 每个主表行的关联键
    for r in base_rows:
        i = bt["columns"].index(on_base)
        key_vals.append(norm(r[i] if i < len(r) else ""))

    detail = [{"dataset": ds["name"], "sheet": base.get("sheet"), "used_rows": len(base_rows),
               "total_rows": bt["rows"], "role": "主表"}]
    skipped = []

    for j in req.get("joins") or []:
        jds = by_id.get(j.get("dataset_id"))
        if not jds:
            continue
        try:
            jt = source_table(jds, j.get("sheet"), j.get("header_rows"), j.get("kind") or "auto")
        except Exception as e:
            skipped.append({"dataset": jds["name"], "sheet": j.get("sheet"), "reason": str(e)})
            continue
        on_join = j.get("on") or ""
        if on_join not in jt["columns"]:
            skipped.append({"dataset": jds["name"], "reason": "找不到关联字段 %s" % on_join})
            continue
        colmap = j.get("mapping") or {}
        if not colmap:
            taken = set(out_cols)
            colmap = {}
            for c in jt["columns"]:
                if c == on_join:
                    continue
                out = c
                if out in taken:
                    out = "%s(%s)" % (c, jds.get("lab") or jds["name"][:6])
                taken.add(out)
                colmap[c] = out
        for c in jt["columns"]:
            if c == on_join:
                continue
            tgt = colmap.get(c)
            if tgt and tgt not in out_cols:
                out_cols.append(tgt)

        idx = {c: i for i, c in enumerate(jt["columns"])}
        ji = idx[on_join]
        lookup = {}
        for r in jt["records"]:
            k = norm(r[ji] if ji < len(r) else "")
            if k and k not in lookup:
                lookup[k] = r
        hits = 0
        for n, r in enumerate(base_rows):
            m = lookup.get(key_vals[n])
            if m:
                hits += 1
                r.extend([""] * (len(out_cols) - len(r)))
                for sc, tc in colmap.items():
                    if tc and sc in idx and idx[sc] < len(m):
                        ti = out_cols.index(tc)
                        while len(r) <= ti:
                            r.append("")
                        if r[ti] == "":
                            r[ti] = m[idx[sc]]
        for r in base_rows:
            r.extend([""] * (len(out_cols) - len(r)))
        detail.append({"dataset": jds["name"], "lab": jds.get("lab", ""), "sheet": j.get("sheet"),
                       "used_rows": hits, "total_rows": jt["rows"], "role": "关联表",
                       "on": "%s = %s" % (on_base, on_join), "matched": hits})

    rows = [list(r) for r in base_rows]

    if dedup:
        seen, uniq = set(), []
        for r in rows:
            k = tuple(r)
            if k not in seen:
                seen.add(k)
                uniq.append(r)
        rows = uniq

    if sort.get("field") and sort["field"] in out_cols:
        i = out_cols.index(sort["field"])
        rows.sort(key=lambda r: str(r[i] if i < len(r) else ""), reverse=bool(sort.get("desc")))

    truncated = False
    if limit and len(rows) > limit:
        rows = rows[:limit]
        truncated = True

    return {"columns": out_cols, "rows": rows, "detail": detail, "skipped": skipped,
            "truncated": truncated, "mode": "join"}


# ---------------------------------------------------------------- 结果池
_results = {}
_result_seq = 0
# 透视结果缓存：同一份结果 + 同一套参数不必重算（导出时会再走一次同样的计算）
_pivot_cache = {}


def _result_file(rid):
    return os.path.join(RESULT_DIR, "%s.json.gz" % rid)


def _prune_results():
    """只保留最近 KEEP_RESULTS 份结果（内存 + 磁盘）"""
    try:
        files = [os.path.join(RESULT_DIR, f) for f in os.listdir(RESULT_DIR)
                 if f.endswith(".json.gz")]
        files.sort(key=lambda f: os.path.getmtime(f))
        for f in files[:-KEEP_RESULTS]:
            try:
                os.remove(f)
            except Exception:
                pass
    except Exception:
        pass


def put_result(columns, rows, meta=None):
    """
    结果池。同时写内存 + 磁盘：
    这样即使关掉程序重新打开，之前生成的汇总结果依然能导出，
    不会出现“结果已过期，请重新生成”。
    """
    global _result_seq
    _result_seq += 1
    rid = "r%d_%s" % (_result_seq, uuid.uuid4().hex[:6])
    rec = {"columns": columns, "rows": rows, "meta": meta or {}, "at": time.time()}
    _results[rid] = rec
    if len(_results) > KEEP_RESULTS:
        for k in sorted(_results, key=lambda x: _results[x]["at"])[:-KEEP_RESULTS]:
            _results.pop(k, None)
    try:
        with gzip.open(_result_file(rid), "wt", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, default=str)
        _prune_results()
    except Exception:
        traceback.print_exc()
    return rid


def get_result(rid):
    if not rid:
        return None
    hit = _results.get(rid)
    if hit:
        return hit
    fp = _result_file(rid)
    if os.path.isfile(fp):
        try:
            with gzip.open(fp, "rt", encoding="utf-8") as f:
                rec = json.load(f)
            _results[rid] = rec          # 回填内存缓存
            return rec
        except Exception:
            return None
    return None


def result_count():
    """持久化汇总结果份数（磁盘为准，重启后仍在）"""
    try:
        return len([f for f in os.listdir(RESULT_DIR) if f.endswith(".json.gz")])
    except Exception:
        return len(_results)


def result_list():
    """历史汇总结果清单（内存池，含启动时从磁盘读回的），供界面独立选择分析/出报告"""
    out = []
    for rid, rec in _results.items():
        meta = rec.get("meta") or {}
        out.append({
            "id": rid,
            "title": meta.get("title") or "汇总结果",
            "mode": meta.get("mode") or "",
            "rows": len(rec.get("rows") or []),
            "cols": len(rec.get("columns") or []),
            "at": rec.get("at") or 0,
        })
    out.sort(key=lambda x: x["at"], reverse=True)
    return out


def tat_analyze(columns, rows, start_col, end_col, group_col=""):
    """TAT（周转时间）分析：结束时间列 − 开始时间列 的时长统计。

    纯读操作，不改任何数据。返回：总体 KPI（均值/中位数/P90/极值，单位小时）、
    时长分布（分桶）、按分组列（如 模块/项目名称）的分组统计。
    """
    from . import report_templates as RT

    cols = list(columns or [])
    rows = list(rows or [])

    def idx(name):
        try:
            return cols.index(name)
        except ValueError:
            return -1

    si, ei = idx(start_col), idx(end_col)
    if si < 0 or ei < 0:
        raise ValueError("请选择有效的时间列（开始 / 结束）")
    if si == ei:
        raise ValueError("开始与结束时间列不能相同")
    gi = idx(group_col) if group_col else -1

    total = valid = invalid = 0
    secs, groups = [], {}
    for r in rows:
        total += 1
        sv = RT.parse_datetime(r[si] if si < len(r) else None)[0]
        ev = RT.parse_datetime(r[ei] if ei < len(r) else None)[0]
        if not sv or not ev:
            invalid += 1
            continue
        d = (ev - sv).total_seconds()
        if d < 0:
            invalid += 1
            continue
        valid += 1
        secs.append(d)
        g = "未分组"
        if gi >= 0 and gi < len(r) and r[gi] not in (None, ""):
            g = str(r[gi])
        groups.setdefault(g, []).append(d)

    def pct(sorted_secs, p):
        if not sorted_secs:
            return None
        k = min(len(sorted_secs) - 1, max(0, int(round(p * len(sorted_secs))) - 1))
        return sorted_secs[k]

    ss = sorted(secs)
    stats = {
        "total": total,
        "valid": valid,
        "invalid": invalid,
        "avg_h": round(sum(ss) / len(ss) / 3600, 2) if ss else None,
        "median_h": round(pct(ss, 0.5) / 3600, 2) if ss else None,
        "p90_h": round(pct(ss, 0.9) / 3600, 2) if ss else None,
        "min_h": round(ss[0] / 3600, 2) if ss else None,
        "max_h": round(ss[-1] / 3600, 2) if ss else None,
    }

    # 时长分布（分桶边界：1h / 2h / 4h / 8h / 24h / 48h）
    edges = [60, 120, 240, 480, 1440, 2880]
    labels = ["<1小时", "1-2小时", "2-4小时", "4-8小时", "8-24小时", "1-2天", ">2天"]
    buckets = [0] * len(labels)
    for sec in secs:
        m = sec / 60
        for i, e in enumerate(edges):
            if m < e:
                buckets[i] += 1
                break
        else:
            buckets[-1] += 1
    dist = [
        {"label": l, "count": c, "pct": round(c * 100.0 / valid, 1) if valid else 0}
        for l, c in zip(labels, buckets)
    ]

    grp_out = []
    for g, arr in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:30]:
        a = sorted(arr)
        grp_out.append({
            "name": g,
            "n": len(a),
            "avg_h": round(sum(a) / len(a) / 3600, 2),
            "median_h": round(pct(a, 0.5) / 3600, 2),
            "p90_h": round(pct(a, 0.9) / 3600, 2),
            "max_h": round(a[-1] / 3600, 2),
        })

    return {"ok": True, "start": start_col, "end": end_col, "group": group_col,
            "stats": stats, "dist": dist, "groups": grp_out}


def load_persisted_results():
    """启动时把磁盘上最近的结果读回内存，让老页面上的导出按钮继续可用"""
    n = 0
    try:
        files = [f for f in os.listdir(RESULT_DIR) if f.endswith(".json.gz")]
        files.sort(key=lambda f: os.path.getmtime(os.path.join(RESULT_DIR, f)))
        for fn in files[-KEEP_RESULTS:]:
            rid = fn[:-len(".json.gz")]
            fp = os.path.join(RESULT_DIR, fn)
            try:
                with gzip.open(fp, "rt", encoding="utf-8") as f:
                    _results[rid] = json.load(f)
                n += 1
            except Exception:
                continue
        # 让 rid 序号接着走，避免新旧撞名
        seqs = []
        for rid in _results:
            m = re.match(r"r(\d+)_", rid or "")
            if m:
                seqs.append(int(m.group(1)))
        if seqs:
            globals()["_result_seq"] = max(seqs)
    except Exception:
        traceback.print_exc()
    return n


# ============================================================================
# v2.0.0 「结果分析」：数据概览 + 数据透视
# ----------------------------------------------------------------------------
# 生成汇总结果之后接着做两件事：
#   ① 数据概览 —— 逐列给出类型、缺失、去重、极值，先判断这份结果能不能用；
#   ② 数据透视 —— 按任意字段分组做交叉统计（等价于 Excel 的数据透视表）。
# 全部在本机算完，不联网、不依赖 AI。
#
# ★ 为什么计算全放后端：结果上限 30 万行，整表丢给浏览器做透视会直接卡死页面。
#   这里算完只把前若干行发给前端，所以 1000 行和 30 万行用起来一样快。
# ============================================================================

PROFILE_TOP_N = 5            # 文本列列举"出现最多"的前几个值
PROFILE_MAX_TOPVALUES = 500  # 去重值超过这个数的文本列就不列举了（列了也看不出东西）
MAX_PIVOT_ROWS = 2000        # 透视结果最多回给前端多少行
MAX_PIVOT_GROUPS = 20000     # 分组数上限：超过就报错，提示换个字段
PIVOT_AGGS = ("count", "nunique", "sum", "avg", "max", "min")
PIVOT_AGG_LABEL = {"count": "计数", "nunique": "去重计数", "sum": "求和",
                   "avg": "平均", "max": "最大", "min": "最小"}
BLANK_LABEL = "（空）"

# 日期形态：2026-01-05 / 2026/1/5 / 2026年1月5日 / 2026-01-05 10:30:00
_DATEISH_RE = re.compile(
    r"^\d{4}\s*[-/.年]\s*\d{1,2}(\s*[-/.月]\s*\d{1,2}\s*日?)?([ T]\d{1,2}:\d{2}(:\d{2})?)?$")
# 只认规范的千分位写法，"1,2" 这种不会被误当成数字
_THOUSANDS_RE = re.compile(r"^-?\d{1,3}(,\d{3})+(\.\d+)?$")


def _to_num(v):
    """
    单元格文本 -> 数字；转不了返回 None。

    结果里的数值都是字符串（写入时统一走过 cell_to_text），这里做一次宽松还原：
    允许千分位逗号和全角数字。带单位或百分号的（如「4.62分」「12.5%」）一律不当数值，
    免得把「工号」「评分等级」这类字段错误地加起来。
    """
    if v is None:
        return None
    s = unicodedata.normalize("NFKC", str(v)).strip()
    if not s:
        return None
    t = s.replace(" ", "")
    if _THOUSANDS_RE.match(t):
        t = t.replace(",", "")
    try:
        return float(t)
    except Exception:
        return None


def _fmt_num(x, nd=None):
    """数字 -> 文本。整数不带小数点（5081 而不是 5081.0），小数最多保留 6 位。"""
    if x is None:
        return ""
    if nd is not None:
        return ("%%.%df" % nd) % x
    if abs(x - int(x)) < 1e-9 and abs(x) < 1e15:
        return str(int(x))
    return ("%.6f" % x).rstrip("0").rstrip(".") or "0"


def _grp(x):
    """给整数加千分位；不是整数就原样返回（透视表里让大数字好读）"""
    try:
        f = float(x)
    except Exception:
        return str(x)
    if abs(f - int(f)) < 1e-9 and abs(f) < 1e15:
        return "{:,}".format(int(f))
    return str(x)


def _pct(a, b):
    if not b:
        return "0%"
    return "%.1f%%" % (a * 100.0 / b)


def _natkey(s):
    """自然排序键：让 1月 < 2月 < 10月，而不是字典序的 1月 < 10月 < 2月"""
    return [(0, int(t)) if t.isdigit() else (1, t) for t in re.split(r"(\d+)", str(s))]


def _order_key(s):
    """空值分组永远排在最后，其余按自然序"""
    return (1, []) if s == BLANK_LABEL else (0, _natkey(s))


def profile_result(columns, rows):
    """逐列统计一份结果（缺失 / 类型 / 分布 / 极值），用来判断数据能不能直接用。"""
    cols = list(columns or [])
    rows = rows or []
    nrow = len(rows)
    out = []
    n_missing = 0
    n_numeric = 0

    for i, name in enumerate(cols):
        # 逐列扫。不做整表 zip(*rows) 转置 —— 30 万行 × 60 列转置要吃掉几百 MB 内存。
        col = [r[i] if i < len(r) else "" for r in rows]

        nonempty = empty = n_date = 0
        nums = []
        total_len = 0
        counter = Counter()
        for v in col:
            s = "" if v is None else str(v).strip()
            if not s:
                empty += 1
                continue
            nonempty += 1
            total_len += len(s)
            counter[s] += 1
            x = _to_num(s)
            if x is not None:
                nums.append(x)
            elif _DATEISH_RE.match(s):
                n_date += 1

        distinct = len(counter)
        avg_len = (total_len / nonempty) if nonempty else 0
        kind, label, summary, note = "text", "文本", "", ""

        if nonempty == 0:
            kind, label, summary = "empty", "空列", "整列没有值"
        elif len(nums) >= 0.9 * nonempty:
            # 非空值里九成以上能转成数字，才算数值列 —— 阈值留一点余量，
            # 容忍个别写着"暂无"的格子，但不能容忍半列文字被当数字算。
            kind, label = "number", "数值"
            n = len(nums)
            ns = sorted(nums)
            total = sum(nums)
            mid = ns[n // 2] if n % 2 else (ns[n // 2 - 1] + ns[n // 2]) / 2.0
            summary = ("合计 %s · 均值 %s · 最小 %s · 最大 %s · 中位 %s"
                       % (_grp(_fmt_num(total)), _fmt_num(total / n, 2),
                          _grp(_fmt_num(ns[0])), _grp(_fmt_num(ns[-1])),
                          _grp(_fmt_num(mid))))
            # 全是整数、位数还多、取值又不少 → 大概率是编号（工号/批次号/订单号），
            # 求和跟平均都没有意义。只做提示，不阻止用户真去算。
            # 两个条件任一即可：取值数够多（大表），或取值占比够高（小表）。
            ints = [x for x in nums if abs(x - int(x)) < 1e-9]
            if (len(ints) == n
                    and (distinct >= 10 or distinct * 2 >= nonempty)
                    and max(len(str(abs(int(x)))) for x in ints) >= 6):
                note = "疑似编号，求和/平均通常没有意义"
        elif n_date >= 0.8 * nonempty:
            kind, label = "date", "日期"
            dk = []
            for k in counter:
                if _DATEISH_RE.match(k):
                    dk.append((tuple(int(t) for t in re.findall(r"\d+", k)[:3]), k))
            if dk:
                dk.sort(key=lambda t: t[0])
                summary = "最早 %s · 最晚 %s" % (dk[0][1], dk[-1][1])
            else:
                summary = "共 %s 个不同取值" % _grp(distinct)
        elif avg_len > 40 or distinct > PROFILE_MAX_TOPVALUES:
            # 长文本 / 取值太散的列，列举 Top N 没有意义，给出规模即可
            kind, label = "longtext", "长文本"
            summary = "平均 %d 字，内容分散（%s 种不同写法）" % (round(avg_len), _grp(distinct))
        else:
            tops = counter.most_common(PROFILE_TOP_N)
            summary = "；".join("%s（%s）" % (k, _pct(c, nonempty)) for k, c in tops)
            if distinct > len(tops):
                summary += "　等 %s 种取值" % _grp(distinct)

        if empty:
            n_missing += 1
        if kind == "number":
            n_numeric += 1
        out.append({
            "name": name, "kind": kind, "kind_label": label,
            "nonempty": nonempty, "empty": empty, "distinct": distinct,
            "summary": summary, "note": note,
        })

    parts = ["共 %s 行 × %d 列" % (_grp(nrow), len(cols))]
    if n_missing:
        worst = max([c for c in out if c["empty"]], key=lambda c: c["empty"])
        parts.append("%d 列存在缺失，其中「%s」缺 %s 条（%s）"
                     % (n_missing, worst["name"], _grp(worst["empty"]),
                        _pct(worst["empty"], nrow)))
    else:
        parts.append("所有列都没有缺失")
    if n_numeric:
        parts.append("%d 个数值列可直接做求和/均值统计" % n_numeric)
    else:
        parts.append("没有识别出数值列（都是文本，透视时可用「计数」）")

    return {
        "rows": nrow, "cols": len(cols),
        "missing_cols": n_missing, "numeric_cols": n_numeric,
        "columns": out, "summary": "；".join(parts) + "。",
    }


def pivot_result(columns, rows, row_dims=None, col_dim="", value="", agg="count",
                 show_total=True, drop_blank_keys=False, max_rows=MAX_PIVOT_ROWS):
    """
    按字段分组做交叉统计（相当于 Excel 的数据透视表）。

    max_rows 只限制"回给界面显示多少行"（默认 2000，界面翻不动那么多）。
    ★ 导出时必须传 max_rows=None —— 否则导出的文件会跟界面一样被截断，
      用户拿到手的 Excel 会静默少掉后面那些分组，这是不能接受的数据丢失。
      分组数本身另有 MAX_PIVOT_GROUPS 兜底，不怕导出撑爆。
    """
    cols = list(columns or [])
    idx = {}
    for i, c in enumerate(cols):
        idx.setdefault(c, i)

    row_dims = [d for d in (row_dims or []) if d in idx][:3]
    col_dim = col_dim if col_dim in idx else ""
    value = value if value in idx else ""
    agg = agg if agg in PIVOT_AGGS else "count"
    if not row_dims and not col_dim:
        raise ValueError("至少要指定一个「行维度」或「列维度」，否则没有分组依据。")
    if agg != "count" and not value:
        raise ValueError("选「%s」时必须指定一个字段 —— 说明要统计的是哪一列的值。"
                         % PIVOT_AGG_LABEL[agg])

    ri = [idx[d] for d in row_dims]
    ci = idx[col_dim] if col_dim else None
    vi = idx[value] if value else None

    def cell(r, i):
        if i is None:
            return ""
        v = r[i] if i < len(r) else ""
        return "" if v is None else str(v).strip()

    buckets = {}
    row_order, col_order = [], []
    seen_row, seen_col = set(), set()
    n_used = n_blank_rows = n_bad_value = 0

    for r in rows or []:
        rk = tuple(cell(r, i) for i in ri) or ("__ALL__",)
        if drop_blank_keys and all(k == "" for k in rk):
            n_blank_rows += 1
            continue
        rk = tuple(BLANK_LABEL if k == "" else k for k in rk)
        ck = (cell(r, ci) or BLANK_LABEL) if ci is not None else "__ALL__"

        if rk not in seen_row:
            if len(seen_row) >= MAX_PIVOT_GROUPS:
                raise ValueError(
                    "按这些字段分组太细了（已超过 %d 组），再算下去表格会大到没法看。\n\n"
                    "建议：① 换一个分类字段当行维度（比如用「实验室名称」，别用「需求描述」）；"
                    "② 少选几个行维度；③ 或者在生成汇总表时先用筛选条件缩小范围。"
                    % MAX_PIVOT_GROUPS)
            seen_row.add(rk)
            row_order.append(rk)
        if ck not in seen_col:
            seen_col.add(ck)
            col_order.append(ck)

        b = buckets.get((rk, ck))
        if b is None:
            b = buckets[(rk, ck)] = {"n": 0, "nn": 0, "s": 0.0,
                                     "mn": None, "mx": None, "st": set()}
        b["n"] += 1
        if agg == "nunique":
            b["st"].add(cell(r, vi))
        elif agg != "count":
            x = _to_num(cell(r, vi))
            if x is None:
                n_bad_value += 1
            else:
                b["nn"] += 1
                b["s"] += x
                b["mn"] = x if b["mn"] is None else min(b["mn"], x)
                b["mx"] = x if b["mx"] is None else max(b["mx"], x)
        n_used += 1

    col_keys = (["__ALL__"] if ci is None
                else sorted((k for k in col_order if k != "__ALL__"), key=_order_key))

    def val_of(b):
        if b is None:
            return ""
        if agg == "count":
            return _grp(b["n"])
        if agg == "nunique":
            return _grp(len(b["st"]))
        if b["nn"] == 0:
            return ""
        if agg == "sum":
            return _grp(_fmt_num(b["s"]))
        if agg == "avg":
            return _fmt_num(b["s"] / b["nn"], 2)
        if agg == "max":
            return _grp(_fmt_num(b["mx"]))
        return _grp(_fmt_num(b["mn"]))

    def merge(acc, b):
        """把一格累加进合计。求和的合计是真求和、平均的合计是"总/总"，
        不是"各格平均值的平均"，所以这里累加的是分子分母而不是结果。"""
        if not b:
            return acc
        acc["n"] += b["n"]
        acc["nn"] += b["nn"]
        acc["s"] += b["s"]
        if b["mn"] is not None:
            acc["mn"] = b["mn"] if acc["mn"] is None else min(acc["mn"], b["mn"])
        if b["mx"] is not None:
            acc["mx"] = b["mx"] if acc["mx"] is None else max(acc["mx"], b["mx"])
        if agg == "nunique":
            acc["st"] |= b["st"]
        return acc

    def new_acc():
        return {"n": 0, "nn": 0, "s": 0.0, "mn": None, "mx": None, "st": set()}

    head = list(row_dims)
    if ci is None:
        head.append("%s（%s）" % (value or "行数", PIVOT_AGG_LABEL[agg]))
    else:
        head.extend(col_keys)
    multi_col = len(col_keys) > 1
    if show_total and multi_col:
        head.append("合计")

    body = []
    for rk in row_order:
        acc = new_acc()
        line = list(rk)
        for ck in col_keys:
            b = buckets.get((rk, ck))
            merge(acc, b)
            line.append(val_of(b))
        if show_total and multi_col:
            line.append(val_of(acc))
        body.append(line)

    total_row = None
    if show_total and len(row_order) > 1:
        grand = new_acc()
        line = ["总计"] + [""] * (len(row_dims) - 1)
        for ck in col_keys:
            colacc = new_acc()
            for rk in row_order:
                merge(colacc, buckets.get((rk, ck)))
            line.append(val_of(colacc))
            merge(grand, colacc)
        if multi_col:
            line.append(val_of(grand))
        total_row = line

    # max_rows=None 表示不截断（导出走这条路，绝不能少行）
    truncated = max_rows is not None and len(body) > max_rows
    shown = body[:max_rows] if truncated else body
    out_rows = shown + ([total_row] if total_row else [])

    return {
        "columns": head,
        "rows": out_rows,
        "total": len(out_rows),
        "groups": len(row_order),
        "shown": len(shown),
        "truncated": truncated,
        "meta": {
            "row_dims": row_dims, "col_dim": col_dim, "value": value,
            "agg": agg, "agg_label": PIVOT_AGG_LABEL[agg],
            "n_col_keys": len(col_keys), "used_rows": n_used,
            "blank_rows": n_blank_rows, "bad_value": n_bad_value,
            "show_total": show_total, "row_limit": max_rows,
        },
    }


# ============================================================================
# v2.1.0 分析报告
#   把汇总结果渲染成一份独立 HTML 报告。计算在 report_templates.py，
#   渲染在 report_render.py —— 两个模块都只依赖 (columns, rows)，不碰 Excel。
# ============================================================================

def report_templates():
    """给界面用的模板清单（含每个模板的参数定义）。"""
    from . import report_templates as RT
    out = []
    for t in RT.TEMPLATES:
        out.append({
            "id": t["id"], "name": t["name"], "desc": t["desc"],
            "icon": t["icon"], "output": t["output"],
            "needs": list(RT.TEMPLATE_FIELDS.get(t["id"], ())),
            "opts": [dict(o) for o in t.get("opts", [])],
        })
    return out


def report_check(columns, template_id):
    """某模板在当前数据下能不能用、缺哪列；顺带把识别到的列名回报给界面。"""
    from . import report_templates as RT
    fr = RT.field_report(columns, template_id)
    found = RT.detect_fields(columns)
    return {
        "ok": fr["ok"],
        "needs": list(RT.TEMPLATE_FIELDS.get(template_id, ())),
        "found": found,
        "found_label": fr.get("found_label") or {},
        "missing": fr["missing"],
    }


def report_generate(rid, template_id, opts=None, name=""):
    """跑一个报告模板：结果池取数 → 计算 → 渲染 → 写进「分析报告」目录。

    返回 {path, dir, file, size, template, kpis}。kpis 给界面弹结果用。
    """
    from . import report_templates as RT
    from . import report_render as RR

    res = get_result(rid)
    if not res:
        raise ValueError(
            "找不到这份汇总结果了（结果编号 %s）。\n"
            "请回到「汇总提取」页，重新点一次「生成汇总表」，再来生成报告。"
            % (rid or "空"))

    columns, rows = res["columns"], res["rows"]
    tpl = RT.TEMPLATE_BY_ID.get(template_id)
    if not tpl:
        raise ValueError("没有这个报告模板：%s" % template_id)

    # opts 里如果是空字符串/None，就当没填，让模板用默认值
    clean = {}
    for k, v in (opts or {}).items():
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        clean[k] = v

    data = RT.build(columns, rows, template_id, clean)

    if template_id == "equip_load":
        html = RR.render_equip_load(data)
    elif template_id == "speed_chain":
        html = RR.render_speed_chain(data)
    else:
        raise ValueError("模板 %s 还没有渲染器" % template_id)

    # 文件名：模板自带命名 + 日期；重名就加序号，不覆盖历史报告
    date_s = (datetime.now().strftime("%Y%m%d")
              if data.get("date_guess") is None else data["date_guess"])
    base = (tpl["output"] or "分析报告_{date}.html").replace("{date}", date_s)
    base = re.sub(r'[\\/:*?"<>|]', "_", base)
    if not base.lower().endswith(".html"):
        base += ".html"
    if name:
        base = re.sub(r'[\\/:*?"<>|]', "_", name) + ".html"
    os.makedirs(REPORT_DIR, exist_ok=True)
    fp = os.path.join(REPORT_DIR, base)
    n = 1
    while os.path.exists(fp):
        stem, ext = os.path.splitext(base)
        fp = os.path.join(REPORT_DIR, "%s(%d)%s" % (stem, n, ext))
        n += 1

    with open(fp, "w", encoding="utf-8") as f:
        f.write(html)

    return {
        "path": fp, "dir": REPORT_DIR, "file": os.path.basename(fp),
        "size": os.path.getsize(fp), "template": template_id,
        "template_name": tpl["name"],
        "kpis": _report_kpis(data),
    }


def _report_kpis(d):
    """从报告数据里抽几条给界面展示的摘要。

    ★ 多天数据（v2.4.0）：界面上展示的「峰值时段/峰值」必须取**最忙那一天**的数字，
    不能拿跨天合并后的合计（那样 7 天里同一小时会被叠成 1 个大峰值，严重夸大）。
    单天数据行为不变。
    """
    base = {"row_count": d.get("row_count"), "html_kb": None}
    multi = bool(d.get("multi_day"))
    n_days = d.get("n_days") or 1
    if multi:
        base["天数"] = n_days
    if d.get("template") == "equip_load":
        if multi:
            # 逐日里挑完成量最大的那天，展示它自己的峰值时段与峰值
            days = d.get("days") or []
            busiest = max(days, key=lambda x: x.get("grand") or 0) if days else None
            if busiest:
                base.update({
                    "总完成量": d.get("grand"),
                    "最忙一天": busiest.get("label"),
                    "当天完成量": busiest.get("grand"),
                    "当天峰值时段": busiest.get("peak_slot") or "—",
                    "当天峰值": busiest.get("peak_val"),
                    "模块数": len(d.get("mod_list") or []),
                    "过载点数": len(d.get("overload") or []),
                })
            else:
                base.update({"总完成量": d.get("grand"),
                             "模块数": len(d.get("mod_list") or [])})
        else:
            base.update({
                "总完成量": d.get("grand"), "峰值时段": d.get("peak_slot"),
                "峰值": d.get("peak_val"), "模块数": len(d.get("mod_list") or []),
                "时段数": len(d.get("slots") or []),
                "过载点数": len(d.get("overload") or []),
            })
    else:
        wl = d.get("hot_long") or []
        kpi = {
            "标称速度": d.get("nominal"),
            "饱和窗": d.get("win_label"), "窗口时长": d.get("win_hours"),
            "满转模块": d.get("hot_label"),
            "实际速度下限": ("%.0f–%.0f" % (min(wl), max(wl))) if wl else "—",
            "短时峰值": max(d.get("hot_peak") or [0]) or 0,
            "模块数": len(d.get("mod_list") or []),
        }
        if multi:
            # 速度证据链：速度是按「最忙那天」重建队列算出来的，标注证据来源
            kpi["证据来自"] = d.get("best_day_label") or "—"
            kpi["有饱和窗天数"] = sum(
                1 for x in (d.get("days_cmp") or [])
                if x.get("speed_long") is not None)
        base.update(kpi)
    return base


def report_list():
    """「分析报告」目录下已有的报告，按时间倒序。"""
    out = []
    if not os.path.isdir(REPORT_DIR):
        return out
    for fn in os.listdir(REPORT_DIR):
        if not fn.lower().endswith(".html"):
            continue
        fp = os.path.join(REPORT_DIR, fn)
        try:
            st = os.stat(fp)
        except OSError:
            continue
        out.append({"file": fn, "path": fp, "size": st.st_size,
                    "mtime": st.st_mtime,
                    "time": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")})
    out.sort(key=lambda x: -x["mtime"])
    return out


def open_report(fn):
    """在浏览器里打开一份报告。只允许打开「分析报告」目录下的文件。"""
    fn = os.path.basename(str(fn or "").strip())
    if not fn:
        raise ValueError("没有指定要打开的报告")
    fp = os.path.abspath(os.path.join(REPORT_DIR, fn))
    if os.path.dirname(fp) != os.path.abspath(REPORT_DIR) or not os.path.isfile(fp):
        raise ValueError("「分析报告」目录里找不到这个文件：%s" % fn)
    webbrowser.open("file:///" + fp.replace("\\", "/"))
    return fp


def _write_table_file(cols, rows, base, fmt="xlsx"):
    """把一张表写进「导出结果」目录，返回文件路径。汇总结果与透视结果共用这一套。"""
    os.makedirs(OUT_DIR, exist_ok=True)
    base = re.sub(r'[\\/:*?"<>|]', "_", base or "汇总结果")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if fmt == "csv":
        fp = os.path.join(OUT_DIR, "%s_%s.csv" % (base, stamp))
        with open(fp, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows(rows)
        return fp
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "汇总结果"
    ws.append(cols)
    hf = Font(bold=True, color="FFFFFF", size=11)
    fill = PatternFill("solid", fgColor="1B5FAA")
    border = Border(*[Side(style="thin", color="C9D6E6")] * 4)
    for c in range(1, len(cols) + 1):
        cell = ws.cell(row=1, column=c)
        cell.font = hf
        cell.fill = fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = border
    ws.row_dimensions[1].height = 26
    for r in rows:
        ws.append([("" if v is None else v) for v in r])
    for i, c in enumerate(cols, start=1):
        width = max(10, min(40, max([len(str(c))] + [len(str(r[i - 1])) for r in rows[:400] if i - 1 < len(r)] or [10]) + 3))
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"
    if rows:
        ws.auto_filter.ref = "A1:%s%d" % (get_column_letter(len(cols)), len(rows) + 1)
    fp = os.path.join(OUT_DIR, "%s_%s.xlsx" % (base, stamp))
    wb.save(fp)
    return fp


def export_result(rid, fmt="xlsx", name=""):
    res = get_result(rid)
    if not res:
        raise ValueError(
            "找不到这份汇总结果了（结果编号 %s）。\n"
            "请回到「汇总提取」页，重新点一次「生成汇总表」，再点导出。" % (rid or "空"))
    return _write_table_file(res["columns"], res["rows"],
                             name or res["meta"].get("title") or "汇总结果", fmt)


# ============================================================================
# v2.2.0 「原始数据预览」里直接编辑 / 删除
# ----------------------------------------------------------------------------
# ★ 核心约束：**只改内存，绝不写回磁盘上的 Excel**。
#   用户在界面上改完，导出成一份新文件落到「导出结果」目录；
#   关掉程序就还原成原样。原始底稿（library/sources 里的副本）永远不动。
#
# 为什么这样设计：
#   · library/sources 里那份是"数据源"，汇总/报告/透视全都依赖它。
#     就地改它 = 悄悄改变了所有下游结果的输入，出了问题很难回溯。
#   · 用户明确的取舍是"最安全、不碰底稿"。
#
# 数据模型 —— 一张"编辑覆盖层"，不复制整张表：
#   _EDITS[sheetkey] = {
#     "cells":   {(row, col): value},   # 单元格改值（含清空 → 存 None）
#     "del_rows": set(),                # 被删的原始行号（0-based）
#     "del_cols": set(),                # 被删的原始列号（0-based）
#     "cleared":  bool,                 # 整个工作表被清空
#     "ops":     [ ... ],               # 操作日志，供 undo/redo 回放
#   }
#
#   sheetkey = "<dataset_id>||<sheet>"
#
# 为什么用"集合 + 映射"而不是"存一份改完的二维数组"：
#   · 表格可能几万行，每次改一格就复制整张表太浪费；
#   · 删行删列用"原始行号/列号"记录，就能和"改值"解耦 ——
#     改了第 5 行、又删了第 3 行，第 5 行不会错位。
#   · 每次渲染时用「原始行 × 原始列」实时投影出结果，语义最清楚。
# ============================================================================

_EDITS = {}                     # sheetkey -> 编辑覆盖层
_DEL_SHEETS = {}                # dataset_id -> set(被删掉的工作表名)
# ★ 数据集级的"删子表"操作日志（undo/redo 用）。
#   为什么不和 _EDITS 一样记在 sheet 上：删子表是**数据集级**动作，而且
#   用户完全可能先在 A 表删掉 B 表、再切到 C 表删掉 D 表 —— 如果各记各的
#   日志，那就是两套互不相干的撤销栈，用户按 undo 只能撤回"当前表发起的"
#   那一次，另一个删除永远撤不掉（实测确实如此）。
#   放在数据集级 => 所有子表共享一条撤销时间线，符合直觉。
#   _DEL_JOURNAL[dataset_id] = {"ops": [sheettab_op...], "redo": [...]}
_DEL_JOURNAL = {}
_EDIT_OP_LIMIT = 400            # 单个工作表最多记多少步操作（undo 深度）
MAX_EDIT_EXPORT_ROWS = 200000   # 编辑后导出的一次性上限，防内存炸


def _journal(dataset_id, create=False):
    if dataset_id not in _DEL_JOURNAL and create:
        _DEL_JOURNAL[dataset_id] = {"ops": [], "redo": []}
    return _DEL_JOURNAL.get(dataset_id)


_seq_counter = [0]


def _next_seq():
    """全局单调递增序号 —— 用来比较"单元格 op"和"删子表 op"谁更晚。"""
    _seq_counter[0] += 1
    return _seq_counter[0]


def _sheet_key(dataset_id, sheet):
    return "%s||%s" % (dataset_id, sheet)


def _get_del_sheets(dataset_id, create=False):
    """这个数据集里被「删掉」的工作表名集合。

    为什么不塞进 _EDITS：_EDITS 是**按 sheet** 建键的，
    而"整张表被删了"这件事属于**数据集级别**的信息 —— 被删的 sheet
    恰恰是没有 _EDITS 条目的那个。硬塞进去会造出一个键对不上的孤儿条目。
    """
    if dataset_id not in _DEL_SHEETS and create:
        _DEL_SHEETS[dataset_id] = set()
    return _DEL_SHEETS.get(dataset_id, set())


def _live_sheets(dataset_id, pth):
    """该数据集实际还剩哪些工作表（原始列表 − 被删的）。"""
    delset = _get_del_sheets(dataset_id)
    return [s for s in sheet_names(pth) if s not in delset]


def _blank_edit():
    # "redo" 必须**跟着状态走**，不能是 raw_edit_apply 的局部变量：
    #   前端是「一次 undo = 一个请求，一次 redo = 另一个请求」，
    #   局部变量在两个请求之间就没了 → redo 永远空、静默失效。
    return {"cells": {}, "del_rows": set(), "del_cols": set(),
            "cleared": False, "ops": [], "redo": []}


def _get_edit(dataset_id, sheet, create=False):
    k = _sheet_key(dataset_id, sheet)
    if k not in _EDITS and create:
        _EDITS[k] = _blank_edit()
    return _EDITS.get(k)


def _prune_edit(dataset_id, sheet):
    """覆盖层彻底空了（没改动、没 ops、没 redo）就把整条记录删掉。

    ★ 为什么必须做：撤销到底之后留下的空壳会**干扰跨 sheet 的 undo/redo 兜底**
      （_find_undo_source 只看"这个 sheet 有没有 ops"，空壳会让它以为
      这张表还有事可做，于是永远轮不到别的表里真正待撤销的 op）。
    """
    k = _sheet_key(dataset_id, sheet)
    e = _EDITS.get(k)
    if not e:
        return
    if (not e["cells"] and not e["del_rows"] and not e["del_cols"]
            and not e["cleared"] and not e["ops"] and not e.get("redo")):
        _EDITS.pop(k, None)


def _edit_dirty(e):
    """这个覆盖层是否真的改过东西。"""
    if not e:
        return False
    return bool(e["cells"] or e["del_rows"] or e["del_cols"] or e["cleared"])


def _dataset_dirty(dataset_id, e):
    """整个数据集有没有未保存的改动。

    ★ 必须把"删子表"也算进来：它是数据集级操作，不落在任何一个 sheet 的
      _EDITS 里。只看 _edit_dirty 的话，用户**只删了子表**时 dirty 会是 False，
      于是「丢弃编辑」「导出为新文件」两个按钮都是灰的 —— 删了子表既撤不回
      也导不出（实测就是这个症状）。
    """
    return _edit_dirty(e) or bool(_get_del_sheets(dataset_id))


def _project(rows, e):
    """把「原始行 × 编辑覆盖层」投影成当前应该显示的表。

    返回 (新行列表, 原始行号列表, 列号列表)。
    原始行号/列号是给前端做定位用的 —— 删了几行之后，屏幕上的第 1 行
    可能是原始的第 3 行，得让前端知道它到底在改哪一行。
    """
    n_raw_cols = max((len(r) for r in rows), default=0)
    if e:
        if e["cleared"]:
            return [], [], []
        cols = [c for c in range(n_raw_cols) if c not in e["del_cols"]]
        delr = e["del_rows"]
        cells = e["cells"]
        out, out_idx = [], []
        for ri, r in enumerate(rows):
            if ri in delr:
                continue
            row = []
            for ci in cols:
                if (ri, ci) in cells:
                    row.append(cells[(ri, ci)])
                else:
                    # ★ 短行必须补 None 再取：真实 Excel 里常有"某行就填了前几列"，
                    #   直接 r[ci] 会 IndexError（原本只读预览靠 _cache 阶段补齐，
                    #   这里投影时必须自己兜住）。
                    row.append(r[ci] if ci < len(r) else None)
            out.append(row)
            out_idx.append(ri)
        return out, out_idx, cols
    # 无编辑：仍然要按 n_raw_cols 补宽，否则短行会让前端 <td> 数量对不上表头。
    return ([list(r) + [None] * (n_raw_cols - len(r)) for r in rows],
            list(range(len(rows))), list(range(n_raw_cols)))


def raw_preview_edit(dataset_id, sheet, offset, size):
    """带编辑态的原始预览：在页面里展示"改完之后长什么样"。"""
    ds = next((d for d in STATE["datasets"] if d["id"] == dataset_id), None)
    if not ds:
        raise ValueError("数据集不存在")
    pth = ds_abs_path(ds)
    all_names = sheet_names(pth)
    # ★ 被删掉的子表不再出现在标签条里 —— 这就是"删子表"的可见效果
    names = _live_sheets(dataset_id, pth)
    if not sheet or sheet not in names:
        # 请求的 sheet 可能刚好被删了（或压根没传），退到第一张还活着的
        sheet = names[0] if names else ""
    if not sheet:
        raise ValueError("这个文件里已经没有可读的工作表了")
    if not all_names:
        raise ValueError("这个文件里没有可读的工作表")

    all_rows = cached_raw(pth, sheet)
    e = _get_edit(dataset_id, sheet)
    proj, proj_idx, cols = _project(all_rows, e)

    size = max(1, min(size, MAX_PAGE_ROWS))
    offset = max(0, offset)
    total = len(proj)
    page = proj[offset:offset + size]
    page_idx = proj_idx[offset:offset + size]

    # 和只读预览同样的"裁掉末尾空列"保护（防拖过格式的表撑到 16384 列）
    ncol = 0
    for r in proj:
        for i in range(len(r) - 1, -1, -1):
            if r[i] not in (None, ""):
                if i + 1 > ncol:
                    ncol = i + 1
                break
    if ncol == 0:
        ncol = min(len(cols), 1)
    ncol = min(ncol, RAW_PREVIEW_MAX_COLS)

    shown_cols = cols[:ncol]
    page = [list(r)[:ncol] + [None] * (max(0, ncol - len(r))) for r in page]

    # 改动标记：把"屏幕上这些格里哪些被改过"告诉前端，好标黄
    marks = []
    if e:
        for si, ri in enumerate(page_idx):
            for ci in range(ncol):
                if (ri, shown_cols[ci]) in e["cells"]:
                    marks.append([si, ci])
    return {
        "ok": True,
        "rows": page,
        "row_idx": page_idx,          # 每行对应的原始行号（0-based）
        "col_idx": shown_cols,        # 每列对应的原始列号（0-based）
        "sheets": names,              # 还活着的子表（被删的已剔除）
        "all_sheets": all_names,      # 文件里原本的全部子表（供前端判断"能不能再删"）
        "del_sheets": sorted(_get_del_sheets(dataset_id)),
        "sheet": sheet,
        "offset": offset,
        "size": size,
        "total": total,
        "total_raw": len(all_rows),
        "cols": ncol,
        "merges": [] if _edit_dirty(e) else cached_merges(pth, sheet),
        "edits": marks,               # [[行, 列], ...] 相对于本页
        "dirty": _dataset_dirty(dataset_id, e),   # ★ 含"删子表"
        "removed_rows": len(e["del_rows"]) if e else 0,
        "removed_cols": len(e["del_cols"]) if e else 0,
        "cleared": bool(e and e["cleared"]),
        "truncated": offset + len(page) < total,
    }


def _push_op(e, op):
    # 每条 op 都带一个全局序号：undo 时要在「单元格 op」和「数据集级删子表 op」
    # 之间挑更晚的那条来撤，没有序号就没法比较先后。
    op.setdefault("seq", _next_seq())
    e["ops"].append(op)
    if len(e["ops"]) > _EDIT_OP_LIMIT:
        del e["ops"][:len(e["ops"]) - _EDIT_OP_LIMIT]


def _sheet_of_edit(dataset_id, e):
    """反查某个覆盖层对象属于哪个 sheet。"""
    pfx = dataset_id + "||"
    for k, v in _EDITS.items():
        if v is e and k.startswith(pfx):
            return k[len(pfx):]
    return None


def raw_edit_apply(dataset_id, sheet, ops):
    """提交一批编辑操作。

    ops 里每一项：
      {"t":"set",  "r":原始行号, "c":原始列号, "v":新值}   改一个格
      {"t":"clear","r":.., "c":..}                        清空一个格
      {"t":"row",  "r":原始行号}                           删整行
      {"t":"col",  "c":原始列号}                           删整列
      {"t":"sheet"}                                        清空整个工作表
      {"t":"sheettab","name":"Sheet3"}                     删掉一整个子表（工作表）
      {"t":"undo"} / {"t":"redo"}                          撤销 / 重做
    """
    if not isinstance(ops, list) or not ops:
        raise ValueError("没有要提交的编辑")
    if len(ops) > 5000:
        raise ValueError("一次提交的改动太多了（上限 5000 项）")

    ds = next((d for d in STATE["datasets"] if d["id"] == dataset_id), None)
    if not ds:
        raise ValueError("数据集不存在")
    pth = ds_abs_path(ds)
    # ★ 校验用「还活着的」工作表：已经被删掉的子表不能再往里写编辑
    if not sheet or sheet not in _live_sheets(dataset_id, pth):
        raise ValueError("工作表不存在或已被删除：%s" % sheet)

    e = _get_edit(dataset_id, sheet, create=True)
    # 兼容老会话里没有 "redo" 键的编辑对象
    if "redo" not in e:
        e["redo"] = []
    redo = e["redo"]              # ★ 持久化的 redo 栈（跨请求存活），见 _blank_edit 注释
    done = 0

    for op in ops:
        if not isinstance(op, dict):
            continue
        t = op.get("t")

        if t == "undo":
            # ★ 撤销要看**两条**时间线，选"更晚发生的那一条"来撤：
            #   · 当前 sheet 自己的单元格级 op（e["ops"] 的最后一条）
            #   · 数据集级的"删子表" op（_DEL_JOURNAL，跨子表共享）
            #   前端每次 undo 只带当前 sheet，所以这里必须自己判断该退哪个。
            jr = _journal(dataset_id)
            tab_op = jr["ops"][-1] if (jr and jr["ops"]) else None
            cell_op = e["ops"][-1] if e["ops"] else None

            if not tab_op and not cell_op:
                continue
            # 谁的 seq 大谁就更晚 —— 后发生的先撤
            if tab_op and (not cell_op
                           or tab_op.get("seq", 0) >= cell_op.get("seq", 0)):
                jr["ops"].pop()
                jr["redo"].append(tab_op)
                _revert_op(e, tab_op, dataset_id)
            else:
                if "redo" not in e:
                    e["redo"] = []
                e["ops"].pop()
                e["redo"].append(cell_op)
                _revert_op(e, cell_op, dataset_id)
            _prune_edit(dataset_id, sheet)
            done += 1
            continue

        if t == "redo":
            jr = _journal(dataset_id)
            tab_op = jr["redo"][-1] if (jr and jr["redo"]) else None
            cell_op = redo[-1] if redo else None
            if not tab_op and not cell_op:
                continue
            if tab_op and (not cell_op
                           or tab_op.get("seq", 0) >= cell_op.get("seq", 0)):
                jr["redo"].pop()
                jr["ops"].append(tab_op)
                _replay_op(e, tab_op, dataset_id)
            else:
                if "redo" not in e:
                    e["redo"] = []
                redo.pop()
                e["ops"].append(cell_op)
                _replay_op(e, cell_op, dataset_id)
            done += 1
            continue

        redo.clear()                 # 任何新编辑都作废 redo 栈（原地清，保住引用）
        if t == "set":
            r, c = int(op.get("r", -1)), int(op.get("c", -1))
            if r < 0 or c < 0:
                continue
            v = op.get("v")
            if isinstance(v, str) and v == "":
                v = None
            # ★ 记录必须同时存 "改之前" 和 "改之后"：
            #   had/old 用于撤销（还原成原样），v/new 用于重做（再写回去）。
            #   只存 old 的话 redo 就没有值可写了（曾踩过这个坑）。
            rec = {"t": "set", "r": r, "c": c,
                   "had": (r, c) in e["cells"],
                   "old": e["cells"].get((r, c)),
                   "new": v}
            e["cells"][(r, c)] = v
            e["cleared"] = False
            _push_op(e, rec); done += 1
        elif t == "clear":
            r, c = int(op.get("r", -1)), int(op.get("c", -1))
            if r < 0 or c < 0 or (r, c) not in e["cells"]:
                continue
            rec = {"t": "clear", "r": r, "c": c,
                   "old": e["cells"].pop((r, c)), "new": None}
            _push_op(e, rec); done += 1
        elif t == "row":
            r = int(op.get("r", -1))
            if r < 0 or r in e["del_rows"]:
                continue
            e["del_rows"].add(r)
            _push_op(e, {"t": "row", "r": r}); done += 1
        elif t == "col":
            c = int(op.get("c", -1))
            if c < 0 or c in e["del_cols"]:
                continue
            e["del_cols"].add(c)
            _push_op(e, {"t": "col", "c": c}); done += 1
        elif t == "sheet":
            if e["cleared"]:
                continue
            rec = {"t": "sheet", "prev": {
                "cells": dict(e["cells"]),
                "del_rows": set(e["del_rows"]),
                "del_cols": set(e["del_cols"]),
            }}
            e["cleared"] = True
            e["cells"] = {}; e["del_rows"] = set(); e["del_cols"] = set()
            _push_op(e, rec); done += 1
        elif t == "sheettab":
            # 删掉一整个「子表」（工作表）。这是**数据集级**操作：
            # op 记进 _DEL_JOURNAL（所有子表共享一条撤销时间线），
            # 而不是记在当前 sheet 的日志里 —— 否则"在 A 表删 B、再切到 C 表
            # 删 D"会变成两条互不相干的撤销栈，用户按 undo 撤不掉第二次删除。
            nm = op.get("name")
            if not nm or not isinstance(nm, str):
                continue
            if nm not in sheet_names(pth):
                raise ValueError("要删的工作表不存在：%s" % nm)
            delset = _get_del_sheets(dataset_id, create=True)
            if nm in delset:
                continue
            # ★ 必须留最后一张：全删光的话预览就没得看了，也没有"回到哪张"可言
            if len([s for s in sheet_names(pth) if s not in delset]) <= 1:
                raise ValueError("至少要留一个工作表，不能把子表全删光。")
            delset.add(nm)
            # 被删子表自己的编辑覆盖层一并丢弃（都没了，留着没意义也容易脏）
            _EDITS.pop(_sheet_key(dataset_id, nm), None)
            jr = _journal(dataset_id, create=True)
            jr["redo"].clear()       # 新动作作废"删子表"的 redo 栈
            jr["ops"].append({"t": "sheettab", "name": nm,
                              "was_current": (nm == sheet),
                              "seq": _next_seq()})
            if len(jr["ops"]) > _EDIT_OP_LIMIT:
                del jr["ops"][0]
            done += 1

    # 撤销/重做到底之后可能只剩个空壳 —— 收尾清掉，别让空壳挡住跨 sheet 兜底
    _prune_edit(dataset_id, sheet)

    _jr = _journal(dataset_id)
    return {
        "ok": True, "applied": done,
        "dirty": _dataset_dirty(dataset_id, e),   # ★ 含"删子表"
        # 可撤销 / 可重做要看**两条**时间线：当前表的单元格 op + 数据集级删子表 op
        "can_undo": bool(e["ops"] or (_jr and _jr["ops"])),
        "can_redo": bool(redo or (_jr and _jr["redo"])),
        "removed_rows": len(e["del_rows"]),
        "removed_cols": len(e["del_cols"]),
        "cleared": e["cleared"],
        "changed": len(e["cells"]),
        "del_sheets": sorted(_get_del_sheets(dataset_id)),
        "sheets": _live_sheets(dataset_id, pth),
    }


def _revert_op(e, op, dataset_id=None):
    """撤销一步操作（还原成"操作之前"的样子）。

    dataset_id 只有 "sheettab" 这个数据集级 op 才用得上（要改 _DEL_SHEETS）。
    """
    t = op.get("t")
    if t == "set":
        # 之前没有这个格 → 删掉；之前有 → 写回旧值
        if op.get("had"):
            e["cells"][(op["r"], op["c"])] = op["old"]
        else:
            e["cells"].pop((op["r"], op["c"]), None)
    elif t == "clear":
        # clear 撤销 = 把原来那个值写回去
        e["cells"][(op["r"], op["c"])] = op["old"]
    elif t == "row":
        e["del_rows"].discard(op["r"])
    elif t == "col":
        e["del_cols"].discard(op["c"])
    elif t == "sheet":
        p = op["prev"]
        e["cells"] = dict(p["cells"])
        e["del_rows"] = set(p["del_rows"])
        e["del_cols"] = set(p["del_cols"])
        e["cleared"] = False
    elif t == "sheettab":
        # 撤销"删子表" = 把它放回去
        if dataset_id is not None:
            _get_del_sheets(dataset_id, create=True).discard(op.get("name"))


def _replay_op(e, op, dataset_id=None):
    """重做一步操作（把它"再做一遍"）。"""
    t = op.get("t")
    if t == "set":
        e["cells"][(op["r"], op["c"])] = op.get("new")
        e["cleared"] = False
    elif t == "clear":
        e["cells"].pop((op["r"], op["c"]), None)
    elif t == "row":
        e["del_rows"].add(op["r"])
    elif t == "col":
        e["del_cols"].add(op["c"])
    elif t == "sheet":
        e["cleared"] = True
        e["cells"] = {}; e["del_rows"] = set(); e["del_cols"] = set()
    elif t == "sheettab":
        # 重做"删子表" = 再删一次
        if dataset_id is not None:
            _get_del_sheets(dataset_id, create=True).add(op.get("name"))


def raw_edit_reset(dataset_id, sheet="", all_sheets=False):
    """丢弃编辑（指定 sheet 或整份数据集）。

    `all_sheets=False`（默认）：`sheet` 非空时**只丢该 sheet 的单元格级改动**；
    "删子表"是数据集级操作，此时不动 —— 否则"丢一张表的编辑"会莫名其妙把
    别的子表变回来，很反直觉。

    `all_sheets=True`：界面上的「**丢弃编辑**」按钮走这条 —— 它承诺的是
    "丢弃**本次的全部**编辑，恢复成原始数据"，所以**被删掉的子表也要放回来**，
    否则按钮文案与实际行为不符（曾漏掉这一点，实测删了子表后点它子表不回来）。
    """
    if sheet and not all_sheets:
        _EDITS.pop(_sheet_key(dataset_id, sheet), None)
    else:
        for k in list(_EDITS):
            if k.startswith(dataset_id + "||"):
                _EDITS.pop(k, None)
        _DEL_SHEETS.pop(dataset_id, None)     # ★ 整份重置：被删的子表也放回来
        _DEL_JOURNAL.pop(dataset_id, None)    # 数据集级的删子表撤销栈也一并清空
    return {"ok": True,
            "sheets": _live_sheets(dataset_id, ds_abs_path(
                next(d for d in STATE["datasets"] if d["id"] == dataset_id)))
            if any(d["id"] == dataset_id for d in STATE["datasets"]) else [],
            "del_sheets": sorted(_get_del_sheets(dataset_id))}


def _write_book_file(book, base):
    """把「多个工作表」写进一个 xlsx，落到「导出结果」目录。

    book = [(sheet_name, cols, rows), ...]
    ★ csv 不支持多表，所以这个只走 xlsx 分支。
    """
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    os.makedirs(OUT_DIR, exist_ok=True)
    base = re.sub(r'[\\/:*?"<>|]', "_", base or "原始数据_已编辑")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    wb = openpyxl.Workbook()
    wb.remove(wb.active)                     # 用自己建的表，不留默认空表
    hf = Font(bold=True, color="FFFFFF", size=11)
    fill = PatternFill("solid", fgColor="1B5FAA")
    border = Border(*[Side(style="thin", color="C9D6E6")] * 4)

    used = set()                             # Excel 不允许重名 sheet，且名字有长度/字符限制
    for name, cols, rows in book:
        title = _safe_sheet_title(name, used)
        ws = wb.create_sheet(title=title)
        ws.append(cols)
        for c in range(1, len(cols) + 1):
            cell = ws.cell(row=1, column=c)
            cell.font = hf; cell.fill = fill
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = border
        ws.row_dimensions[1].height = 26
        for r in rows:
            ws.append([("" if v is None else v) for v in r])
        for i, c in enumerate(cols, start=1):
            width = max(10, min(40, max(
                [len(str(c))] + [len(str(r[i - 1])) for r in rows[:400] if i - 1 < len(r)]
                or [10]) + 3))
            ws.column_dimensions[get_column_letter(i)].width = width
        ws.freeze_panes = "A2"
        if rows:
            ws.auto_filter.ref = "A1:%s%d" % (get_column_letter(len(cols)), len(rows) + 1)

    fp = os.path.join(OUT_DIR, "%s_%s.xlsx" % (base, stamp))
    wb.save(fp)
    return fp


def _safe_sheet_title(name, used):
    """把工作表名收拾成 Excel 能接受的（去非法字符、截 31 字、去重）。"""
    t = re.sub(r'[\\/:*?\[\]]', "_", str(name or "Sheet")).strip() or "Sheet"
    t = t[:31]
    if t not in used:
        used.add(t)
        return t
    for i in range(2, 999):
        suffix = "_%d" % i
        cand = t[:31 - len(suffix)] + suffix
        if cand not in used:
            used.add(cand)
            return cand
    return t[:28] + "_x"


def raw_edit_export(dataset_id, sheet, fmt="xlsx", name=""):
    """把「编辑后」的表导出成一份**新文件**，落到「导出结果」目录。

    ★ 绝不覆盖、也绝不写回 library/sources 里的原始文件。

    v2.2.0 起支持"删子表"：默认导出**剩下所有子表**（各自一个工作表，
    写进同一个 xlsx）；被删掉的不导出。csv 只支持单表 —— 这时导当前
    正在看的这一张，并在返回里说明。
    """
    ds = next((d for d in STATE["datasets"] if d["id"] == dataset_id), None)
    if not ds:
        raise ValueError("数据集不存在")
    pth = ds_abs_path(ds)
    live = _live_sheets(dataset_id, pth)
    if not live:
        raise ValueError("已经没有可导出的工作表了")
    if sheet and sheet not in live:
        # 请求的 sheet 已被删 → 退到第一张还活着的
        sheet = live[0]

    base = (name or "").strip() or ("%s_已编辑" % os.path.splitext(ds["name"])[0])

    def _sheet_payload(nm):
        rows = cached_raw(pth, nm)
        e = _get_edit(dataset_id, nm)
        proj, _idx, _cols = _project(rows, e)
        if len(proj) > MAX_EDIT_EXPORT_ROWS:
            raise ValueError("「%s」编辑后有 %d 行，超过导出的安全上限（%d 行）。"
                             "请先删掉不需要的行再导出。"
                             % (nm, len(proj), MAX_EDIT_EXPORT_ROWS))
        ncol = max((len(r) for r in proj), default=0)
        # 列名用 Excel 的 A/B/C —— 原始预览本来就是"照原样看"，
        # 第一行往往不是表头，硬编"表头"反而误导。
        cols = [_excel_col_name(i) for i in range(ncol)]
        body = [list(r) + [None] * (ncol - len(r)) for r in proj]
        return cols, body

    # ---- csv：只支持单表，导当前这张 ----
    if fmt == "csv":
        nm = sheet or live[0]
        cols, body = _sheet_payload(nm)
        fp = _write_table_file(cols, body, "%s_%s" % (base, nm), "csv")
        return {
            "ok": True, "path": fp, "dir": OUT_DIR, "file": os.path.basename(fp),
            "size": os.path.getsize(fp), "rows": len(body), "cols": len(cols),
            "fmt": "csv", "edited": True,
            "exported_sheets": [nm], "skipped_sheets": [],
            "note": "csv 一次只能存一张表，这里是「%s」。想一次带走所有子表请选 xlsx。" % nm,
        }

    # ---- xlsx：把剩下的所有子表写进同一个工作簿 ----
    book, total_rows = [], 0
    for nm in live:
        cols, body = _sheet_payload(nm)
        book.append((nm, cols, body))
        total_rows += len(body)
    fp = _write_book_file(book, base)
    return {
        "ok": True, "path": fp, "dir": OUT_DIR, "file": os.path.basename(fp),
        "size": os.path.getsize(fp),
        "rows": total_rows, "cols": len(book[0][1]) if book else 0,
        "fmt": "xlsx", "edited": True,
        "exported_sheets": list(live),
        "skipped_sheets": sorted(_get_del_sheets(dataset_id)),
    }


def _excel_col_name(i):
    """0 -> A, 25 -> Z, 26 -> AA"""
    s = ""
    i += 1
    while i > 0:
        r = (i - 1) % 26
        s = chr(65 + r) + s
        i = (i - 1) // 26
    return s


def raw_edit_save(dataset_id, sheet="", backup=True):
    """把「编辑后」的结果**写回原始文件**（library/sources 里的那一份）。

    ★ v2.3.0 新增。这**推翻了 v2.2.0 的"绝不写回"约束** —— 是用户明确要求
      "编辑完后可以直接保存并替换为原来的文件"。三条确认过的取舍：
        ① 覆盖前**自动备份**原文件到 BAK_DIR（带时间戳）；
        ② 写回后**自动重新识别**该文件，刷新索引；
        ③ **删掉的列不写回**（与编辑语义一致：删了就是删了）。

    安全设计（覆盖是不可逆的，所以每一步都得能兜住）：
      · 先备份再动手。备份失败 ⇒ 直接抛错，绝不继续覆盖。
      · 写进同目录的临时文件再 `os.replace` 原子替换 —— 中途崩了原文件还是完整的。
        （openpyxl 直接 save 到目标路径会先把文件截断，写到一半失败就毁数据。）
      · 保留原文件的权限位（stat 里的 mode），避免替换后权限被重置。
      · 写前先清 `_raw_cache`，保证 `_sheet_payload` 拿到的"原始行"不是缓存的旧值。
      · 行数护栏与导出同一套（MAX_EDIT_EXPORT_ROWS）。
      · 全程持 `_lock`（RLock，_lock 与 save_state 共用，可重入）。

    写回成功后：
      · 清空该数据集的 `_EDITS` / `_DEL_SHEETS` / `_DEL_JOURNAL` —— 改动已经进文件了，
        留着覆盖层会让界面显示的"编辑态"与磁盘真实内容对不上（用户再点一次保存
        等于把改动**又叠一遍**）。
      · `reparse_dataset()` 刷新工作表列表 / 表头 / 列名 / 行数。
      · `STATE["recognize_version"]` 跟着置位，避免下次启动触发全量重算。
    """
    with _lock:
        ds = next((d for d in STATE["datasets"] if d["id"] == dataset_id), None)
        if not ds:
            raise ValueError("数据集不存在")
        dst = ds_abs_path(ds)
        if not os.path.isfile(dst):
            raise ValueError("原始文件已不在（可能被手工删了）：%s" % ds.get("name"))
        if os.path.splitext(dst)[1].lower() in (".csv", ".txt"):
            # csv 没有"工作表"的概念，多表回写无从谈起，且极易搞坏分隔符/编码。
            raise ValueError("csv 文件不支持就地保存，请用「导出为新文件」。")

        live = _live_sheets(dataset_id, dst)
        if not live:
            raise ValueError("不能把子表全删光，至少要留一个。")

        # ---- 先把每张表的编辑结果算出来（这一步不碰磁盘）----
        # ★ 必须先把 _raw_cache 里这个文件的条目清掉：删子表后我们改的是
        #   内存注册表，但 cached_raw 按 (path, sheet, mtime) 缓存，
        #   万一之前读过，拿到的可能是"另一套"数据。清掉最稳。
        _purge_raw_cache(dst)

        book, total_rows = [], 0
        for nm in live:
            rows = cached_raw(dst, nm)
            e = _get_edit(dataset_id, nm)
            proj, _idx, _cols = _project(rows, e)
            if len(proj) > MAX_EDIT_EXPORT_ROWS:
                raise ValueError("「%s」编辑后有 %d 行，超过就地保存的安全上限（%d 行）。"
                                 "请先删掉不需要的行。" % (nm, len(proj), MAX_EDIT_EXPORT_ROWS))
            # 列宽取"保留下来最宽的那一列"，否则删掉宽列后整表会挤成一条
            ncol = max((len(r) for r in proj), default=0)
            body = [list(r) + [None] * (ncol - len(r)) for r in proj]
            book.append((nm, body))
            total_rows += len(body)

        # ---- 1) 备份（失败就不许覆盖）----
        bak = ""
        if backup:
            os.makedirs(BAK_DIR, exist_ok=True)
            stem = os.path.splitext(os.path.basename(dst))[0]
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            bak = os.path.join(BAK_DIR, "%s_%s.xlsx" % (stem, stamp))
            # 同一秒内连点两次保存也不覆盖前一份备份
            n = 2
            while os.path.exists(bak):
                bak = os.path.join(BAK_DIR, "%s_%s_%d.xlsx" % (stem, stamp, n))
                n += 1
            try:
                shutil.copy2(dst, bak)
                if os.path.getsize(bak) <= 0:
                    raise IOError("备份文件是空的")
            except Exception as e:
                raise ValueError("备份原始文件失败，已中止保存（原始文件未被改动）：%s" % e)

        old_mode = None
        try:
            old_mode = os.stat(dst).st_mode
        except OSError:
            pass

        # ---- 2) 写临时文件 ----
        tmp = dst + ".slh_tmp.xlsx"
        try:
            _write_book_to_path(book, tmp)
        except Exception as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise ValueError("写入失败，原始文件未被改动：%s" % e)

        # ---- 3) 原子替换 ----
        try:
            os.replace(tmp, dst)
        except Exception as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise ValueError("替换原始文件失败（原始文件未被改动）：%s" % e)
        if old_mode is not None:
            try:
                os.chmod(dst, old_mode)
            except OSError:
                pass

        # ---- 4) 改动已落盘，覆盖层作废 ----
        for k in list(_EDITS):
            if k.startswith(dataset_id + "||"):
                _EDITS.pop(k, None)
        _DEL_SHEETS.pop(dataset_id, None)
        _DEL_JOURNAL.pop(dataset_id, None)
        _purge_raw_cache(dst)
        _cache.clear()

        # ---- 5) 重解析 + 刷新索引 ----
        # ★ 文件**已经写成功**了，所以这里出错不能把整个 save 判为失败
        #   （否则用户会以为没存上、再点一次 → 改动叠第二遍）。
        #   正确姿态：照常返回 ok，但把"索引没刷新成功"这件事显式带回去，
        #   前端可以提示"数据已保存，但重新识别失败，请手动重新导入"。
        sheets = []
        reparse_err = ""
        try:
            sheets = sheet_names(dst)
            reparse_dataset(ds)
            STATE["recognize_version"] = RECOGNIZE_VERSION
            save_state()
        except Exception as e:
            reparse_err = "%s: %s" % (type(e).__name__, e)
            traceback.print_exc()

        ok_rows = sum(s.get("rows", 0) for s in ds.get("sheets", []))
        return {
            "ok": True,
            "reparse_error": reparse_err,
            "file": os.path.basename(dst),
            "path": dst,
            "backup": bak,
            "backup_dir": BAK_DIR,
            "saved_sheets": list(live),
            "sheet_count": len(sheets) or len(live),
            "rows": total_rows,          # 实际写进文件的合计行数
            "index_rows": ok_rows,       # 重解析后索引里认到的合计行数
            "size": os.path.getsize(dst),
            "mtime": os.stat(dst).st_mtime,
            "dataset": ds_summary(ds),
            "cleared_edits": True,
        }


# ============================================================================
# 测试脚手架（**仅供自动化测试用**，界面上不会调用）
# ----------------------------------------------------------------------------
# 为什么需要它：v2.3.0 的「保存并替换原文件」会**真的覆盖文件**。
# 浏览器端到端测试又想验证"点下去之后文件真的变了"，那就绝不能在用户的
# 真实数据表上做 —— 于是提供这三个接口，让测试能：
#   ① 把某个数据集复制成一次性副本（save_clone）
#   ② 从后端复核副本的状态（save_verify）
#   ③ 跑完把副本文件 / 备份 / 索引条目删干净（save_cleanup）
# 副本的 id 统一带 "ds_clone_" 前缀，cleanup 只认这个前缀，误删不了别的。
# 生产使用中不会碰到这三个路由（界面里没有任何调用点）。
# ============================================================================
_CLONE_PREFIX = "ds_clone_"


def raw_save_clone(dataset_id):
    """复制一个数据集为一次性副本（测试用）。返回副本的 id/路径/子表。"""
    with _lock:
        ds = next((d for d in STATE["datasets"] if d["id"] == dataset_id), None)
        if not ds:
            raise ValueError("数据集不存在")
        src = ds_abs_path(ds)
        if not os.path.isfile(src):
            raise ValueError("原始文件不在")
        cid = _CLONE_PREFIX + uuid.uuid4().hex[:8]
        rel = "sources/%s_%s" % (cid, os.path.basename(src))
        dst = os.path.join(LIB_DIR, rel.replace("/", os.sep))
        shutil.copy2(src, dst)
        clone = {"id": cid, "name": "[测试副本] " + ds.get("name", "?"),
                 "stored": rel, "src_path": dst, "size": os.path.getsize(dst),
                 "imported_at": now_str(), "lab": ds.get("lab", ""),
                 "table_type": ds.get("table_type", ""), "note": "", "sheets": []}
        reparse_dataset(clone)
        STATE["datasets"].append(clone)
        save_state()
        return {"ok": True, "path": dst,
                "dataset": ds_summary(clone),
                "src_sha256": _sha256(src)}


def raw_save_verify(dataset_id):
    """复核副本状态（测试用）：文件里真实有几张表、编辑层清没清、备份对不对。"""
    ds = next((d for d in STATE["datasets"] if d["id"] == dataset_id), None)
    if not ds:
        raise ValueError("数据集不存在")
    pth = ds_abs_path(ds)
    file_sheets = sheet_names(pth) if os.path.isfile(pth) else []
    # ★ 必须遍历**该数据集下所有 sheet key** 才能判断"编辑层清干净了没"。
    #   以前图省事写 `_get_edit(dataset_id, "")` —— 那查的是 "id||" 这个
    #   根本不存在的键，永远返回 None，于是 edits_cleared 恒为 True（假绿）。
    any_edit = False
    for k in _EDITS:
        if k.startswith(dataset_id + "||"):
            e = _EDITS.get(k)
            if e and (e["cells"] or e["del_rows"] or e["del_cols"]
                      or e["cleared"] or e["ops"] or e.get("redo")):
                any_edit = True
                break
    edits_cleared = (not any_edit
                     and not _get_del_sheets(dataset_id)
                     and dataset_id not in _DEL_JOURNAL)
    # 找这个副本最新的备份，比对"备份 == 某次覆盖前的内容"
    backup_ok = False
    baks = []
    if os.path.isdir(BAK_DIR):
        stem = os.path.splitext(os.path.basename(pth))[0]
        baks = sorted([f for f in os.listdir(BAK_DIR) if f.startswith(stem)])
        backup_ok = bool(baks)
    # ★ "文件被改过吗"用**和备份比对**判断：备份就是覆盖前那一刻的原样，
    #   内容不同 ⇒ 这次保存确实改了东西。比"有个布尔常量"有意义得多。
    changed = False
    if baks and os.path.isfile(pth):
        newest = os.path.join(BAK_DIR, baks[-1])
        changed = _sha256(pth) != _sha256(newest)
    return {
        "ok": True,
        "file": os.path.basename(pth),
        "size": os.path.getsize(pth) if os.path.isfile(pth) else 0,
        "mtime": os.stat(pth).st_mtime if os.path.isfile(pth) else 0,
        "file_sheets": file_sheets,
        "sheets": _live_sheets(dataset_id, pth),
        "changed": changed,
        "edits_cleared": edits_cleared,
        "backup_ok": backup_ok,
        "backups": baks,
    }


def raw_save_cleanup(dataset_id):
    """删掉测试副本：索引条目 + 副本文件 + 它产生的备份（测试用）。"""
    with _lock:
        if not str(dataset_id).startswith(_CLONE_PREFIX):
            raise ValueError("只允许清理测试副本（id 需以 %s 开头）" % _CLONE_PREFIX)
        ds = next((d for d in STATE["datasets"] if d["id"] == dataset_id), None)
        pth = ds_abs_path(ds) if ds else ""
        removed_file = False
        if pth and os.path.isfile(pth):
            try:
                os.remove(pth)
                removed_file = True
            except OSError:
                pass
        # 删这个副本产生的备份
        n_bak = 0
        if pth and os.path.isdir(BAK_DIR):
            stem = os.path.splitext(os.path.basename(pth))[0]
            for f in list(os.listdir(BAK_DIR)):
                if f.startswith(stem):
                    try:
                        os.remove(os.path.join(BAK_DIR, f))
                        n_bak += 1
                    except OSError:
                        pass
            if not os.listdir(BAK_DIR):
                try:
                    os.rmdir(BAK_DIR)
                except OSError:
                    pass
        STATE["datasets"] = [d for d in STATE["datasets"] if d["id"] != dataset_id]
        _EDITS.pop(_sheet_key(dataset_id, ""), None)
        for k in list(_EDITS):
            if k.startswith(dataset_id + "||"):
                _EDITS.pop(k, None)
        _DEL_SHEETS.pop(dataset_id, None)
        _DEL_JOURNAL.pop(dataset_id, None)
        if pth:
            _purge_raw_cache(pth)
        save_state()
        return {"ok": True, "removed_file": removed_file, "removed_backups": n_bak}


def _sha256(path):
    import hashlib
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                h.update(b)
        return h.hexdigest()
    except OSError:
        return ""


def _purge_raw_cache(path):
    """把某个文件在 _raw_cache / _merge_cache 里的全部条目清掉。

    这两个缓存是 `(path, sheet, mtime)` 三重键。回写前后 mtime 会变，
    理论上旧键不会再命中；但"删子表"这类改动不落盘、mtime 也不变，
    所以我们宁愿显式清一遍 —— 这条函数只做一件事，别往里加逻辑。
    """
    for k in list(_raw_cache):
        if k[0] == path:
            _raw_cache.pop(k, None)
    for k in list(_merge_cache):
        if k[0] == path:
            _merge_cache.pop(k, None)


def _write_book_to_path(book, path):
    """把多个工作表写进 `path`（**指定路径**，不落「导出结果」）。

    与 `_write_book_file` 的区别只有一个：目标路径可控。
    特意不复用它 —— 那个函数在名字里就写死了"落到导出目录"，
    回写原始文件是另一种语义（覆盖数据源），混在一起容易误用。
    两者样式保持一致（蓝底白字表头 + 列宽 + 冻结首行 + 筛选）。
    """
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    hf = Font(bold=True, color="FFFFFF", size=11)
    fill = PatternFill("solid", fgColor="1B5FAA")
    border = Border(*[Side(style="thin", color="C9D6E6")] * 4)

    used = set()
    for name, body in book:
        title = _safe_sheet_title(name, used)
        ws = wb.create_sheet(title=title)
        for i, row in enumerate(body, start=1):
            ws.append([("" if v is None else v) for v in row])
            if i == 1:
                # ★ 原始预览的第一行是"照原样看"的数据行，不一定是表头。
                #   所以只给它加个浅色底以示"首行"，不套用"表头"的蓝底白字，
                #   免得用户以为程序认错了表头。
                for c in range(1, max(1, len(row)) + 1):
                    cell = ws.cell(row=1, column=c)
                    cell.fill = PatternFill("solid", fgColor="EEF4FB")
                    cell.border = border
        ncol = max((len(r) for r in body), default=0)
        for i in range(1, ncol + 1):
            width = max(10, min(40, max(
                [len(str(r[i - 1])) for r in body[:400] if i - 1 < len(r)]
                or [10]) + 3))
            ws.column_dimensions[get_column_letter(i)].width = width
        if body:
            ws.freeze_panes = "A2"
            try:
                ws.auto_filter.ref = "A1:%s%d" % (get_column_letter(max(1, ncol)), len(body))
            except Exception:
                pass
    wb.save(path)
    try:
        wb.close()
    except Exception:
        pass



def _source_integrity(dataset_id, sheet):
    """给前端/测试用：确认原始文件确实没被编辑功能动过。

    返回该文件的 mtime + 大小 + 行数，用户点了"保存/导出"后可以对比，
    证明"只改内存"这条约束真的生效了。
    """
    ds = next((d for d in STATE["datasets"] if d["id"] == dataset_id), None)
    if not ds:
        raise ValueError("数据集不存在")
    pth = ds_abs_path(ds)
    st = os.stat(pth) if os.path.isfile(pth) else None
    rows = cached_raw(pth, sheet) if sheet and os.path.isfile(pth) else []
    return {
        "ok": True,
        "file": os.path.basename(pth),
        "mtime": st.st_mtime if st else None,
        "size": st.st_size if st else None,
        "rows_raw": len(rows),
    }


def outgoing_file(name):
    """
    取出「导出结果」里的一个文件，准备发给浏览器下载。
    返回 (完整路径, MIME, 建议文件名)。只允许取 OUT_DIR 下的文件，防止越权读盘。
    """
    name = os.path.basename(str(name or "").strip())
    if not name:
        raise ValueError("没有指定要下载的文件名")
    fp = os.path.join(OUT_DIR, name)
    fp = os.path.abspath(fp)
    if os.path.dirname(fp) != os.path.abspath(OUT_DIR) or not os.path.isfile(fp):
        raise ValueError("导出目录里找不到这个文件：%s" % name)
    low = name.lower()
    if low.endswith(".xlsx"):
        ctype = ("application/vnd.openxmlformats-officedocument."
                 "spreadsheetml.sheet")
    elif low.endswith(".csv"):
        ctype = "text/csv; charset=utf-8"
    else:
        ctype = "application/octet-stream"
    return fp, ctype, name


def newest_export(limit=15):
    """
    列出「导出结果」里最近的导出文件。
    返回 {"files": [...], "total": N}：
      · files —— 按修改时间倒序的前 limit 条
      · total —— 该目录下的文件总数（前端据此提示"还有 N 份没列出来"）
    只统计文件，不动子目录（导出目录里若有子文件夹不会被误删）。
    """
    allf = []
    try:
        for f in os.listdir(OUT_DIR):
            fp = os.path.join(OUT_DIR, f)
            if os.path.isfile(fp):
                allf.append({"name": f, "size": os.path.getsize(fp),
                             "mtime": os.path.getmtime(fp)})
    except Exception:
        pass
    allf.sort(key=lambda x: x["mtime"], reverse=True)
    return {"files": allf[:limit], "total": len(allf)}


def exported_path(name):
    """
    把「导出结果」里的一个文件名，解析成可以安全操作的绝对路径。
    只允许 OUT_DIR 下的**直接文件**，挡掉 `../`、绝对路径、子目录等越权写法。
    返回 (绝对路径, 纯文件名)。
    """
    name = os.path.basename(str(name or "").strip())
    if not name:
        raise ValueError("没有指定文件名")
    if name in (".", ".."):
        raise ValueError("文件名不合法")
    fp = os.path.abspath(os.path.join(OUT_DIR, name))
    if os.path.dirname(fp) != os.path.abspath(OUT_DIR):
        raise ValueError("文件名不合法：%s" % name)
    return fp, name


def delete_exports(names):
    """
    删除「导出结果」里的若干文件（用户确认过"连磁盘文件一起删"）。
    返回 (已删除的名字列表, 出错列表)。单个失败不影响其余。
    """
    if not isinstance(names, list):
        names = [names]
    removed, errors = [], []
    for raw in names:
        try:
            fp, nm = exported_path(raw)
        except Exception as e:
            errors.append({"name": str(raw or ""), "error": str(e)})
            continue
        if not os.path.isfile(fp):
            errors.append({"name": nm, "error": "文件已不存在（可能已被删或手工移走）"})
            continue
        try:
            os.remove(fp)
            removed.append(nm)
        except Exception as e:
            errors.append({"name": nm, "error": str(e)})
    return removed, errors


def clear_exports():
    """
    清空「导出结果」里的全部导出文件。
    只删文件、不删子目录 —— 万一用户往这个目录里放了别的东西，不至于被一锅端。
    返回 (已删除的名字列表, 出错列表)。
    """
    removed, errors = [], []
    try:
        for f in os.listdir(OUT_DIR):
            fp = os.path.join(OUT_DIR, f)
            if not os.path.isfile(fp):
                continue
            try:
                os.remove(fp)
                removed.append(f)
            except Exception as e:
                errors.append({"name": f, "error": str(e)})
    except Exception as e:
        errors.append({"name": "", "error": str(e)})
    return removed, errors


# ---------------------------------------------------------------- 钉钉在线文档
DT_EXTS = (".xlsx", ".xlsm", ".csv")
MAX_DT_SCAN = 400


def dingtalk_port():
    """
    钉钉桌面客户端启动后，会在 %APPDATA%\\DingTalk\\mcp_port 里写出本机服务的端口。
    这里把它读出来，用来直连钉钉的本地能力。
    """
    cands = []
    ap = os.environ.get("APPDATA")
    if ap:
        cands.append(os.path.join(ap, "DingTalk", "mcp_port"))
    cands.append(os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "DingTalk", "mcp_port"))
    for fp in cands:
        if not os.path.isfile(fp):
            continue
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                d = json.load(f)
            p = int(d.get("http_port") or 0)
            if p:
                return p
        except Exception:
            continue
    return 0


def dingtalk_dirs():
    """钉钉可能放表格文件的地方（钉盘挂载目录、下载目录、桌面、文档等）"""
    home = os.path.expanduser("~")
    cands = []
    # 钉盘（DingDrive）挂载目录，名字里通常带用户名
    try:
        for d in os.listdir(home):
            if d.lower().startswith("dingdrive") or "钉盘" in d:
                cands.append(os.path.join(home, d))
    except Exception:
        pass
    cands += [
        os.path.join(home, "Documents", "DingTalk"),
        os.path.join(home, "Documents", "钉钉"),
        os.path.join(home, "Downloads"),
        os.path.join(home, "Desktop"),
        os.path.join(home, "Documents"),
    ]
    ap = os.environ.get("APPDATA")
    if ap:
        cands.append(os.path.join(ap, "DingTalk"))
    out, seen = [], set()
    for c in cands:
        k = os.path.abspath(c).lower()
        if os.path.isdir(c) and k not in seen:
            seen.add(k)
            out.append(os.path.abspath(c))
    return out


def dingtalk_scan(dirs=None, limit=MAX_DT_SCAN):
    """扫描钉钉相关目录里的表格文件（递归，但跳过 library 自己）"""
    dirs = dirs or dingtalk_dirs()
    lib_abs = os.path.abspath(LIB_DIR).lower()
    files = []
    for base in dirs:
        if not os.path.isdir(base):
            continue
        for root, subdirs, fs in os.walk(base):
            if os.path.abspath(root).lower().startswith(lib_abs):
                subdirs[:] = []
                continue
            # 跳过明显的缓存/日志目录，免得扫描很慢
            subdirs[:] = [d for d in subdirs
                          if d.lower() not in ("cache", "logs", "log", "dumps", "tmp", "temp")]
            for fn in fs:
                if fn.startswith("~$") or fn.startswith("."):
                    continue
                if os.path.splitext(fn)[1].lower() in DT_EXTS:
                    fp = os.path.join(root, fn)
                    try:
                        files.append({"path": fp, "name": fn, "size": os.path.getsize(fp),
                                      "mtime": os.path.getmtime(fp),
                                      "dir": base})
                    except Exception:
                        pass
                    if len(files) >= limit:
                        break
            if len(files) >= limit:
                break
        if len(files) >= limit:
            break
    files.sort(key=lambda x: x["mtime"], reverse=True)
    return files


def _parse_mcp_response(raw):
    """
    MCP 的响应可能是裸 JSON，也可能是 SSE（每行以 data: 开头）。
    两种都兜住。
    """
    t = (raw or "").strip()
    if not t:
        raise ValueError("钉钉没有返回任何内容")
    if t.startswith("{"):
        return json.loads(t)
    for line in t.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            body = line[5:].strip()
            if body and body != "[DONE]":
                try:
                    return json.loads(body)
                except Exception:
                    continue
    # 最后再试整体解析
    m = re.search(r"\{[\s\S]*\}", t)
    if m:
        return json.loads(m.group(0))
    raise ValueError("看不懂钉钉返回的内容：%s" % t[:200])


def dingtalk_rpc(method, params=None, timeout=90):
    """调用钉钉本机 MCP 服务（JSON-RPC over HTTP）"""
    dt = STATE["config"].get("dingtalk") or {}
    port = int(dt.get("port") or 0) or dingtalk_port()
    token = (dt.get("token") or "").strip()
    if not port:
        raise ValueError("没检测到钉钉本机服务。请先打开并登录钉钉桌面客户端，再点「重新检测」。")
    if not token:
        raise ValueError("还没有填写钉钉 Access Token。装了「钉钉」连接器后，"
                         "把它的访问令牌填到「设置 → 钉钉在线文档」里即可。")
    payload = {"jsonrpc": "2.0", "id": int(time.time() * 1000) % 1000000, "method": method}
    if params is not None:
        payload["params"] = params
    req = urllib.request.Request("http://127.0.0.1:%d/mcp" % port,
                                 data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise ValueError("钉钉接口返回 HTTP %s：%s\n"
                         "（如果提示 access denied，说明 Token 不对或者没装钉钉连接器）" % (e.code, detail))
    except urllib.error.URLError as e:
        raise ValueError("连不上钉钉本机服务（端口 %d）：%s。请确认钉钉客户端正在运行。" % (port, e.reason))
    return _parse_mcp_response(raw)


def dingtalk_save_result(result, tool=""):
    """
    从钉钉 MCP 工具的返回里把表格文件抠出来、存到本程序的数据目录。
    支持两种常见形态：
      · 返回里带一个本地文件路径（钉钉客户端已经下载好了）
      · 返回里带 base64 内容
    抠不出来就返回 None（前端会把原始返回显示出来，方便排查）。
    """
    texts = []

    def walk(o, depth=0):
        if depth > 8:
            return
        if isinstance(o, str):
            texts.append(o)
        elif isinstance(o, dict):
            for v in o.values():
                walk(v, depth + 1)
        elif isinstance(o, list):
            for v in o:
                walk(v, depth + 1)

    walk(result)
    out_dir = os.path.join(LIB_DIR, "dingtalk_pull")
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 1) 返回里直接给了本地文件路径
    for t in texts:
        for m in re.finditer(r'[A-Za-z]:\\[^"\'<>\n\r|?*]+?\.(?:xlsx|xlsm|csv)', t, re.I):
            p = m.group(0).strip()
            if os.path.isfile(p):
                dst = os.path.join(out_dir, "钉钉_%s_%s" % (stamp, os.path.basename(p)))
                try:
                    shutil.copy2(p, dst)
                    return dst
                except Exception:
                    return p

    # 2) 返回里是 base64
    for t in texts:
        m = re.search(r'[A-Za-z0-9+/=\s]{400,}', t)
        if not m:
            continue
        raw = re.sub(r"\s+", "", m.group(0))
        try:
            data = base64.b64decode(raw + "=" * (-len(raw) % 4), validate=False)
        except Exception:
            continue
        if data[:2] == b"PK" or data[:3] == b"\xef\xbb\xbf":
            ext = ".xlsx" if data[:2] == b"PK" else ".csv"
            dst = os.path.join(out_dir, "钉钉_%s%s" % (stamp, ext))
            with open(dst, "wb") as f:
                f.write(data)
            return dst
    return None


def dingtalk_status():
    dt = STATE["config"].get("dingtalk") or {}
    port = int(dt.get("port") or 0) or dingtalk_port()
    dirs = dingtalk_dirs()
    files = dingtalk_scan(dirs)
    return {
        "ok": True,
        "detected_port": port,
        "has_token": bool((dt.get("token") or "").strip()),
        "doc_url": dt.get("doc_url") or "",
        "cli_template": dt.get("cli_template") or "",
        "dirs": dirs[:10],
        "files": len(files),
        "client_found": bool(port),
        "mcp_ready": bool(port and (dt.get("token") or "").strip()),
    }


def dingtalk_pull_via_cli(url, out_dir):
    """
    命令行桥接：按用户填的模板调用外部命令（例如装了钉钉连接器后的 CLI），
    把在线文档导出成本地文件。模板里可用 {url} 和 {out} 两个占位符。
    """
    dt = STATE["config"].get("dingtalk") or {}
    tpl = (dt.get("cli_template") or "").strip()
    if not tpl:
        raise ValueError("还没配置命令行模板。到「设置 → 钉钉在线文档」里填写，"
                         "例如：dingtalk doc export --url \"{url}\" --out \"{out}\"")
    if not url:
        raise ValueError("请先填钉钉文档链接")
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, "钉钉文档_%s.xlsx" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    cmd = tpl.replace("{url}", url).replace("{out}", out_file)
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=180)
    except Exception as e:
        raise ValueError("调用命令失败：%s" % e)
    if r.returncode != 0:
        raise ValueError("命令执行失败（退出码 %s）：%s" % (r.returncode, (r.stderr or r.stdout or "")[:400]))
    if not os.path.isfile(out_file):
        raise ValueError("命令执行完了，但没有生成文件：%s\n命令输出：%s"
                         % (out_file, (r.stdout or "")[:300]))
    return out_file


# ---------------------------------------------------------------- AI
def ai_chat(messages, cfg=None, timeout=180):
    ai = (cfg or STATE["config"]).get("ai") or {}
    if not ai.get("api_key"):
        raise ValueError("尚未配置 AI 的 API Key，请到「AI 助手 / 设置」里填写。")
    url = (ai.get("base_url") or "").rstrip("/") + "/chat/completions"
    payload = {
        "model": ai.get("model") or "deepseek-chat",
        "messages": messages,
        "temperature": float(ai.get("temperature", 0.2)),
        "stream": False,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + ai["api_key"])
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise ValueError("AI 接口返回 %s：%s" % (e.code, detail))
    except urllib.error.URLError as e:
        raise ValueError("无法连接 AI 接口（%s），请检查网络或 Base URL。" % e.reason)
    return body["choices"][0]["message"]["content"]


def ai_context():
    lines = []
    lines.append("【已导入的数据表】")
    for ds in STATE["datasets"]:
        lines.append("- 数据集ID：%s | 文件：%s | 实验室：%s | 表类型：%s" %
                     (ds["id"], ds["name"], ds.get("lab") or "-", ds.get("table_type") or "-"))
        for sh in ds.get("sheets", []):
            lines.append("    工作表「%s」共%d行，字段：%s" %
                         (sh["name"], sh.get("rows", 0), "、".join(sh.get("columns", []))))
    lines.append("")
    lines.append("【全部字段（归一化去重）】")
    fd = field_dictionary()
    lines.append("、".join("%s(出现在%d张表)" % (f["name"], f["dataset_count"]) for f in fd[:120]))
    return "\n".join(lines)


def ai_plan(question):
    sys_prompt = (
        "你是「智慧实验室数据汇总助手」的查询规划器。"
        "用户会用自然语言描述想统计的数据，你要把它翻译成一段 JSON 查询计划。\n"
        "只输出 JSON，不要任何解释、不要 markdown 代码块。\n"
        "JSON 结构：\n"
        '{"mode":"union","source_ids":["数据集ID"],"fields":["输出字段名"],'
        '"filters":[{"field":"字段","op":"contains|eq|ne|gt|lt|empty|notempty|notcontains","value":"值"}],'
        '"add_source_col":true,"dedup":false,"limit":0,"title":"结果名","reason":"一句话说明"}\n'
        "规则：\n"
        "1. mode 用 union（多表纵向合并）。只有用户明确说“按某字段关联/匹配/带出某表的列”时才用 join。\n"
        "2. fields 必须是数据里真实存在的字段名（优先用原样写法），只放用户真正需要的字段；"
        "如果用户说“全部/所有记录”，就给出该数据集主要的字段。\n"
        "3. source_ids 只选与问题相关的数据集；如果用户说的是某个实验室，就选该实验室的数据集。\n"
        "4. 不确定时宁可多选数据集，但 fields 要精准。\n"
        "5. 没有把握的字段不要编造。"
    )
    user_prompt = ai_context() + "\n\n【用户需求】\n" + question
    txt = ai_chat([{"role": "system", "content": sys_prompt},
                   {"role": "user", "content": user_prompt}])
    plan = parse_json_block(txt)
    return plan, txt


def parse_json_block(txt):
    t = txt.strip()
    t = re.sub(r"^```(?:json)?", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    m = re.search(r"\{[\s\S]*\}", t)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    raise ValueError("AI 返回的内容不是有效 JSON，请重试或换个说法。")


def ai_analyze(rid, question):
    res = get_result(rid)
    if not res:
        raise ValueError("结果已过期，请先重新生成汇总")
    cols, rows = res["columns"], res["rows"]
    head = [" | ".join(cols)]
    for r in rows[:60]:
        head.append(" | ".join(str(x) for x in r))
    table_txt = "\n".join(head)
    sys_prompt = (
        "你是迈克生物集团实验室运营管理部的数据分析助手。"
        "请基于给出的汇总数据回答问题，输出中文，可以用小标题和要点，"
        "结论要有数据支撑；如果数据不足以回答，请直接说明还缺什么。不要编造数字。"
    )
    user_prompt = ("【汇总数据】共 %d 行，字段：%s\n%s\n\n【问题】\n%s" %
                   (len(rows), "、".join(cols), table_txt, question))
    return ai_chat([{"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_prompt}])


# ---------------------------------------------------------------- HTTP
MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}


def _pivot_params(b):
    """把前端传来的透视参数收成 pivot_result() 的关键字参数"""
    dims = b.get("row_dims")
    if isinstance(dims, str):
        dims = [dims]
    return {
        "row_dims": [str(x) for x in (dims or []) if x],
        "col_dim": str(b.get("col_dim") or ""),
        "value": str(b.get("value") or ""),
        "agg": str(b.get("agg") or "count"),
        "show_total": bool(b.get("show_total", True)),
        "drop_blank_keys": bool(b.get("drop_blank_keys", False)),
    }


def pivot_cached(rid, params, max_rows=MAX_PIVOT_ROWS):
    """
    算一次透视。同一份结果 + 同一套参数直接走缓存 ——
    前端「生成透视表」和随后的「导出」会带着同样的参数各请求一次，不必重算。

    ★ 行数上限必须进缓存键：「界面显示」限 2000 行、「导出」不限行，
      两者结果根本不一样。要是混用同一份缓存，导出的 Excel 会被悄悄截成 2000 行。
    """
    key = json.dumps({"p": params, "max": max_rows}, sort_keys=True, ensure_ascii=False)
    ck = (rid, max_rows)
    hit = _pivot_cache.get(ck)
    if hit and hit["key"] == key:
        return hit["pv"]
    res = get_result(rid)
    if not res:
        raise ValueError(
            "找不到这份汇总结果了（结果编号 %s）。\n"
            "请回到「汇总提取」页，重新点一次「生成汇总表」。" % (rid or "空"))
    pv = pivot_result(res["columns"], res["rows"], max_rows=max_rows, **params)
    if len(_pivot_cache) > 12:
        _pivot_cache.pop(next(iter(_pivot_cache)))
    _pivot_cache[ck] = {"key": key, "pv": pv}
    return pv


class Handler(BaseHTTPRequestHandler):
    server_version = "SmartLabHub/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str))

    def _download(self, name):
        """
        把文件当作附件发给浏览器（触发真正的“下载”）。
        中文文件名用 RFC 5987 的 filename* 形式，同时给一个 ASCII 兜底名，
        避免 IE/旧浏览器显示乱码或直接失败。
        """
        try:
            fp, ctype, fn = outgoing_file(name)
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 404)
        try:
            with open(fp, "rb") as f:
                data = f.read()
        except Exception as e:
            return self._json({"ok": False, "error": "读取文件失败：%s" % e}, 500)
        ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", fn) or "export"
        disp = "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (
            ascii_name, quote(fn, safe=""))
        # 注意：Content-Length 由 _send 统一设置，这里不能重复发，否则浏览器会中断下载
        self._send(200, data, ctype, {"Content-Disposition": disp})

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    @staticmethod
    def _err_code(e):
        """用户输入问题回 400，程序自身故障才回 500。

        原来一律回 500，浏览器控制台会把"你把测速窗口填成 abc"这种
        正常校验也记成 Internal Server Error，污染控制台、也让人分不清
        "填错了" 和 "程序崩了"。ValueError/KeyError 都是代码里主动抛的
        业务提示，按 4xx 处理；其余（IO、编码、三方库等）保持 500。
        """
        return 400 if isinstance(e, (ValueError, KeyError)) else 500

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        try:
            if p.startswith("/api/"):
                return self.api_get(p, parse_qs(u.query))
            return self.static(p)
        except Exception as e:
            traceback.print_exc()
            self._json({"ok": False, "error": str(e)}, self._err_code(e))

    def do_POST(self):
        u = urlparse(self.path)
        try:
            if u.path.startswith("/api/"):
                return self.api_post(u.path, self._body())
            self._json({"ok": False, "error": "not found"}, 404)
        except Exception as e:
            traceback.print_exc()
            self._json({"ok": False, "error": str(e)}, self._err_code(e))

    def static(self, p):
        if p == "/favicon.ico":
            # 不提供图标，直接回 204，避免浏览器控制台一直报 404
            return self._send(204, b"", "image/x-icon")
        if p in ("/", "", "/index.html"):
            fp = os.path.join(WEB_DIR, "index.html")
        else:
            rel = p.lstrip("/").replace("..", "")
            fp = os.path.join(WEB_DIR, rel)
        if not os.path.isfile(fp):
            return self._send(404, "404", "text/plain; charset=utf-8")
        ext = os.path.splitext(fp)[1].lower()
        with open(fp, "rb") as f:
            data = f.read()
        self._send(200, data, MIME.get(ext, "application/octet-stream"))

    # ---------------- GET
    def api_get(self, p, q):
        if p == "/api/download":
            return self._download((q.get("name") or [""])[0])
        if p == "/api/dingtalk/status":
            return self._json(dingtalk_status())
        if p == "/api/dingtalk/scan":
            return self._json({"ok": True, "files": dingtalk_scan()})
        if p == "/api/exports":
            snap = newest_export()
            return self._json({"ok": True, "out_dir": OUT_DIR,
                               "files": snap["files"], "total": snap["total"]})
        if p == "/api/state":
            return self._json({
                "ok": True,
                "app_version": APP_VERSION,
                "config": get_config(),
                "datasets": [ds_summary(d) for d in STATE["datasets"]],
                "base_dir": BASE_DIR,
                "out_dir": OUT_DIR,
                "report_dir": REPORT_DIR,
                "bak_dir": BAK_DIR,
                "health": library_health(),
            })
        if p == "/api/fields":
            return self._json({"ok": True, "fields": field_dictionary()})
        if p == "/api/preview":
            ds_id = (q.get("dataset_id") or [""])[0]
            sn = (q.get("sheet") or [""])[0]
            hr = (q.get("header_rows") or [""])[0] or None
            ds = next((d for d in STATE["datasets"] if d["id"] == ds_id), None)
            if not ds:
                raise ValueError("数据集不存在")
            if not sn:
                sn = ds["sheets"][0]["name"]
            t = source_table(ds, sn, hr, q.get("kind", ["auto"])[0])
            return self._json({"ok": True, "columns": t["columns"],
                               "rows": t["records"][:80], "total": t["rows"]})
        if p == "/api/raw_preview":
            # 「已导入数据表 → 预览」用的接口：原样还原 Excel 里的内容。
            # 不合并多层表头、不跳过标题行、不丢空行空列 —— 与磁盘上的文件逐格对应。
            # 分页参数 offset/size：默认第 0 行起、每页 100 行。
            ds_id = (q.get("dataset_id") or [""])[0]
            sn = (q.get("sheet") or [""])[0]
            # v2.2.0：带 edit=1 时走 raw_preview_edit —— 在"原样"之上叠加内存编辑层
            # （改过的格、删掉的行列），页面显示"改完之后的样子"。
            # 不带 edit=1 时保持老行为逐字节不变，老调用方与老测试不受影响。
            if (q.get("edit") or [""])[0] in ("1", "true", "yes"):
                return self._json(raw_preview_edit(
                    ds_id, sn,
                    max(0, int((q.get("offset") or ["0"])[0])),
                    int((q.get("size") or ["100"])[0])))
            ds = next((d for d in STATE["datasets"] if d["id"] == ds_id), None)
            if not ds:
                raise ValueError("数据集不存在")
            pth = ds_abs_path(ds)
            names = sheet_names(pth)
            if not sn:
                sn = names[0] if names else ""
            if not sn:
                raise ValueError("这个文件里没有可读的工作表")
            size = int((q.get("size") or ["100"])[0])
            size = max(1, min(size, MAX_PAGE_ROWS))
            offset = max(0, int((q.get("offset") or ["0"])[0]))

            all_rows = cached_raw(pth, sn)
            total = len(all_rows)
            page = all_rows[offset:offset + size]

            # ★ 列数必须按「真实有值的范围」算，不能取 max(len(r))。
            #   坑：Excel 里给整行拖过格式后，行长度会被撑到 16384（Excel 列上限）。
            #   实测某 sheet 是 124 行 × 16384 列 = 203 万格 —— 前端照这个画 <td>
            #   会直接卡死浏览器（几十万 DOM 节点）。
            #   所以这里把"末尾连续全空的列"裁掉，只渲染有内容的列。
            cols_raw = max((len(r) for r in all_rows), default=0)
            ncol = 0
            for r in all_rows:
                for i in range(len(r) - 1, -1, -1):
                    if r[i] not in (None, ""):
                        if i + 1 > ncol:
                            ncol = i + 1
                        break
            if ncol == 0:
                ncol = min(cols_raw, 1)          # 整表全空时也留一列，别给 0
            # 双保险：即便真实值范围很大，也压到 RAW_PREVIEW_MAX_COLS 以内，
            # 超出的列不画（前端会明确提示"只显示前 N 列"）。
            ncol = min(ncol, RAW_PREVIEW_MAX_COLS)

            page = [list(r)[:ncol] + [None] * (max(0, ncol - len(r))) for r in page]

            merges = cached_merges(pth, sn)
            return self._json({
                "ok": True,
                "rows": page,
                "sheets": names,
                "sheet": sn,
                "offset": offset,
                "size": size,
                "total": total,          # 该工作表总行数（含空行）
                "cols": ncol,            # 实际渲染的列数（已裁掉末尾空列）
                "cols_raw": cols_raw,    # 该表原始最大列数（用于提示是否截断）
                "cols_clipped": cols_raw > ncol,
                "merges": merges,        # 合并单元格（A1 记法），供前端做视觉提示
                "truncated": offset + len(page) < total,
            })
        if p == "/api/results":
            return self._json({"ok": True, "results": result_list()})
        if p == "/api/result":
            rid = (q.get("id") or [""])[0]
            offset = int((q.get("offset") or ["0"])[0])
            size = int((q.get("size") or ["200"])[0])
            size = max(1, min(size, MAX_PAGE_ROWS))
            offset = max(0, offset)
            res = get_result(rid)
            if not res:
                raise ValueError("结果已过期")
            return self._json({"ok": True, "columns": res["columns"],
                               "rows": res["rows"][offset:offset + size],
                               "total": len(res["rows"]), "meta": res["meta"],
                               "offset": offset, "size": size})
        if p == "/api/suggest_mapping":
            ids = (q.get("dataset_ids") or [""])[0].split(",")
            ids = [i for i in ids if i]
            names = (q.get("names") or [""])[0].split("\u0001")
            names = [n for n in names if n]
            out = {}
            for ds_id in ids:
                ds = next((d for d in STATE["datasets"] if d["id"] == ds_id), None)
                if not ds:
                    continue
                cols = []
                for sh in ds["sheets"]:
                    for c in sh["columns"]:
                        if c not in cols:
                            cols.append(c)
                out[ds_id] = auto_mapping(cols, names)
            return self._json({"ok": True, "mapping": out})
        return self._json({"ok": False, "error": "unknown api"}, 404)

    # ---------------- POST
    def api_post(self, p, b):
        if p == "/api/import_paths":
            paths = b.get("paths") or []
            if not isinstance(paths, list):
                paths = [paths]
            added, errors = [], []
            for raw in paths:
                pth = (raw or "").strip().strip('"')
                if not pth:
                    continue
                if os.path.isdir(pth):
                    files = []
                    lib_abs = os.path.abspath(LIB_DIR).lower()
                    for root, _dirs, fs in os.walk(pth):
                        if os.path.abspath(root).lower().startswith(lib_abs):
                            continue
                        for fn in fs:
                            if fn.startswith("~$") or fn.startswith("."):
                                continue
                            if os.path.splitext(fn)[1].lower() in (".xlsx", ".xlsm", ".csv"):
                                files.append(os.path.join(root, fn))
                    files.sort()
                    if not files:
                        errors.append({"file": pth, "error": "文件夹里没有 .xlsx/.csv 文件"})
                    for f in files:
                        try:
                            added.append(ds_summary(import_file(f, lab=b.get("lab") or "",
                                                                table_type=b.get("table_type") or "")))
                        except Exception as e:
                            errors.append({"file": f, "error": str(e)})
                else:
                    try:
                        added.append(ds_summary(import_file(pth, lab=b.get("lab") or "",
                                                            table_type=b.get("table_type") or "")))
                    except Exception as e:
                        errors.append({"file": pth, "error": str(e)})
            save_state()
            return self._json({"ok": True, "added": added, "errors": errors})

        if p == "/api/import_recent":
            """
            一键恢复：把之前导入过的来源目录再扫一遍，把表格重新导进来。
            用来救「手滑删掉了数据表」的情况 —— 删除只删程序里的副本，
            原始文件还在，所以能重新捞回来。
            """
            dirs = list(b.get("dirs") or STATE["config"].get("recent_source_dirs") or [])
            dirs = [d for d in dirs if os.path.isdir(d)]
            if not dirs:
                raise ValueError("还没有记录过导入来源。先正常导入一次文件，之后就能一键恢复了。")
            existing = {(d.get("name"), d.get("size")) for d in STATE["datasets"]}
            added, skipped, errors = [], [], []
            for base in dirs:
                for root, _dd, fs in os.walk(base):
                    for fn in sorted(fs):
                        if fn.startswith("~$") or fn.startswith("."):
                            continue
                        if os.path.splitext(fn)[1].lower() not in DT_EXTS:
                            continue
                        full = os.path.join(root, fn)
                        try:
                            sz = os.path.getsize(full)
                        except Exception:
                            continue
                        if (fn, sz) in existing:
                            skipped.append(fn)
                            continue
                        try:
                            added.append(ds_summary(import_file(full, display_name=fn)))
                            existing.add((fn, sz))
                        except Exception as e:
                            errors.append({"file": full, "error": str(e)})
            save_state()
            return self._json({"ok": True, "added": added, "skipped": skipped,
                               "errors": errors, "dirs": dirs})

        if p == "/api/import_upload":
            files = b.get("files") or []
            added, errors = [], []
            for f in files:
                try:
                    name = re.sub(r'[\\/:*?"<>|]', "_", f.get("name") or "upload.xlsx")
                    tmp = os.path.join(SRC_DIR, "_tmp_" + uuid.uuid4().hex[:8] + "_" + name)
                    data = base64.b64decode(f.get("data") or "")
                    with open(tmp, "wb") as fh:
                        fh.write(data)
                    try:
                        ds = import_file(tmp, lab=b.get("lab") or "",
                                         table_type=b.get("table_type") or "", display_name=name)
                        added.append(ds_summary(ds))
                    finally:
                        try:
                            os.remove(tmp)
                        except Exception:
                            pass
                except Exception as e:
                    traceback.print_exc()
                    errors.append({"file": f.get("name"), "error": str(e)})
            save_state()
            return self._json({"ok": True, "added": added, "errors": errors})

        if p == "/api/dataset/update":
            ds = next((d for d in STATE["datasets"] if d["id"] == b.get("id")), None)
            if not ds:
                raise ValueError("数据集不存在")
            for k in ("lab", "table_type", "note"):
                if k in b:
                    ds[k] = b[k] or ""
            if b.get("sheets"):
                for upd in b["sheets"]:
                    for sh in ds["sheets"]:
                        if sh["name"] == upd.get("name"):
                            newhr = str(upd.get("header_rows") or sh.get("header_rows") or "1")
                            if "kind" in upd:
                                sh["kind"] = upd["kind"] or "table"
                            kind = sh.get("kind") or "table"
                            if newhr != str(sh.get("header_rows")) or "kind" in upd:
                                sh["header_rows"] = newhr
                                sh["user_header"] = True if "header_rows" in upd and upd.get("header_rows") else sh.get("user_header", False)
                                try:
                                    if kind == "info":
                                        sh["kind"] = "info"
                                        t = build_info_table(ds_abs_path(ds), sh["name"])
                                    else:
                                        sh["kind"] = "table"
                                        t = build_table(ds_abs_path(ds), sh["name"], newhr)
                                    sh["columns"] = t["columns"]
                                    sh["base_columns"] = t.get("base_columns") or t["columns"]
                                    sh["rows"] = t["rows"]
                                    sh["sample"] = t["records"][:3]
                                    sh["signature"] = "|".join(sorted(norm(c) for c in t["columns"] if norm(c)))
                                    sh.pop("error", None)
                                except Exception as e:
                                    sh["error"] = str(e)
                            if "enabled" in upd:
                                sh["enabled"] = bool(upd["enabled"])
            save_state()
            return self._json({"ok": True, "dataset": ds_summary(ds)})

        if p == "/api/dataset/delete":
            ids = b.get("ids") or [b.get("id")]
            ids = [i for i in ids if i]
            removed = []
            for i in ids:
                ds = next((d for d in STATE["datasets"] if d["id"] == i), None)
                if not ds:
                    continue
                try:
                    fp = ds_abs_path(ds)
                    if os.path.isfile(fp):
                        os.remove(fp)
                except Exception as e:
                    print("删除文件失败：", e)
                STATE["datasets"].remove(ds)
                removed.append(i)
            _cache.clear()
            save_state()
            return self._json({"ok": True, "removed": removed})

        if p == "/api/dataset/clear":
            for ds in list(STATE["datasets"]):
                try:
                    fp = ds_abs_path(ds)
                    if os.path.isfile(fp):
                        os.remove(fp)
                except Exception:
                    pass
            STATE["datasets"] = []
            _cache.clear()
            save_state()
            return self._json({"ok": True})

        if p == "/api/export/delete":
            # 「设置 → 最近导出的文件」删除：用户可以单条删，也可以勾选多条一起删。
            # 同时删掉磁盘上「导出结果」里的实体文件（用户在确认框里看到的语义就是这样）。
            # 只影响程序自己留的备份副本，用户另存到别处的文件不受影响。
            names = b.get("names")
            if names is None:
                names = b.get("name")
            if not names:
                raise ValueError("没有指定要删除的文件")
            removed, errors = delete_exports(names)
            snap = newest_export()
            return self._json({"ok": True, "removed": removed, "errors": errors,
                               "files": snap["files"], "total": snap["total"]})

        if p == "/api/export/clear":
            # 「清空全部」：把导出目录里的导出文件一次清掉。只删文件，不动子目录。
            removed, errors = clear_exports()
            snap = newest_export()
            return self._json({"ok": True, "removed": removed, "errors": errors,
                               "files": snap["files"], "total": snap["total"]})

        if p == "/api/config":
            cfg = STATE["config"]
            if "labs" in b:
                cfg["labs"] = b["labs"] or []
            if "table_types" in b:
                cfg["table_types"] = b["table_types"] or []
            if "mapping" in b:
                # 字段映射记忆：整份覆盖保存，前端只在用户改过之后才发上来
                m = b["mapping"]
                cfg["mapping"] = m if isinstance(m, dict) else {}
            if "dingtalk" in b:
                dt = b["dingtalk"] or {}
                cur = cfg.setdefault("dingtalk", {})
                for k in ("enabled", "port", "doc_url", "cli_template", "download_dirs"):
                    if k in dt:
                        cur[k] = dt[k]
                if dt.get("token"):
                    cur["token"] = dt["token"]
                if dt.get("clear_token"):
                    cur["token"] = ""
            if "ai" in b:
                ai = b["ai"] or {}
                cur = cfg["ai"]
                for k in ("enabled", "base_url", "model", "temperature"):
                    if k in ai:
                        cur[k] = ai[k]
                if ai.get("api_key"):
                    cur["api_key"] = ai["api_key"]
                if ai.get("clear_key"):
                    cur["api_key"] = ""
            save_state()
            return self._json({"ok": True, "config": get_config()})

        if p == "/api/aggregate":
            t0 = time.time()
            res = aggregate(b)
            rid = put_result(res["columns"], res["rows"], {"title": b.get("title") or "汇总结果"})
            return self._json({
                "ok": True, "result_id": rid, "columns": res["columns"],
                "rows": res["rows"][:PREVIEW_ROWS], "total": len(res["rows"]),
                "preview": min(PREVIEW_ROWS, len(res["rows"])),
                "detail": res.get("detail"), "skipped": res.get("skipped"),
                "mode": res.get("mode"), "elapsed": round(time.time() - t0, 2),
            })

        if p == "/api/aggregate_peek":
            # 概览「表头结构分组 → 预览」：把这一组真的合并一遍，但**不落盘**。
            # 与 /api/aggregate 的唯一区别就是"不写结果、不占结果列表"，
            # 让用户在跳去「汇总提取」之前先确认数据长什么样、行数对不对。
            # ★ 刻意不传 limit 给 aggregate()：limit 是"算完再截断"，
            #   一旦带上，返回的 rows 就被切短了，拿不到真实总行数。
            #   预览数据量小（几十行 x 几列），全量算一遍的开销可以接受。
            req = dict(b)
            req.pop("limit", None)
            t0 = time.time()
            res = aggregate(req)
            rows = res["rows"]
            return self._json({
                "ok": True,
                "columns": res["columns"],
                "rows": rows[:PEEK_ROWS],
                "total": len(rows),                    # 真实总行数（合并后）
                "preview": min(PEEK_ROWS, len(rows)),  # 本次实际回了几行
                "peek_rows": PEEK_ROWS,
                "detail": res.get("detail"),           # 每张表各自用了多少行
                "skipped": res.get("skipped"),
                "mode": res.get("mode"),
                "truncated": res.get("truncated"),
                "elapsed": round(time.time() - t0, 2),
            })

        if p == "/api/export":
            rid = b.get("result_id")
            fmt = b.get("format") or "xlsx"
            fp = export_result(rid, fmt, b.get("name") or "")
            return self._json({"ok": True, "path": fp, "dir": os.path.dirname(fp),
                               "file": os.path.basename(fp),
                               "size": os.path.getsize(fp)})

        # ---------------- v2.0.0 结果分析 / 数据透视 ----------------
        if p == "/api/result/profile":
            # 「结果分析 → 数据概览」：逐列统计。纯本地计算，不联网。
            rid = b.get("result_id")
            res = get_result(rid)
            if not res:
                raise ValueError(
                    "找不到这份汇总结果了（结果编号 %s）。\n"
                    "请回到「汇总提取」页，重新点一次「生成汇总表」。" % (rid or "空"))
            return self._json({"ok": True, "profile": profile_result(res["columns"], res["rows"])})

        if p == "/api/result/pivot":
            # 「结果分析 → 数据透视」：按字段分组做交叉统计
            pv = pivot_cached(b.get("result_id"), _pivot_params(b))
            return self._json({"ok": True, **pv})

        if p == "/api/result/pivot_export":
            # 透视结果直接导出。复用 _write_table_file，所以导出格式、备份目录、
            # 浏览器下载的行为跟「导出汇总结果」完全一致。
            # ★ max_rows=None：导出必须全量，不能跟着界面截到 2000 行。
            pv = pivot_cached(b.get("result_id"), _pivot_params(b), max_rows=None)
            fmt = b.get("format") or "xlsx"
            name = b.get("name") or "数据透视"
            fp = _write_table_file(pv["columns"], pv["rows"], name, fmt)
            return self._json({"ok": True, "path": fp, "dir": os.path.dirname(fp),
                               "file": os.path.basename(fp),
                               "size": os.path.getsize(fp),
                               "rows": len(pv["rows"])})

        # ---------------- v2.1.0 分析报告 ----------------
        if p == "/api/report/templates":
            # 模板清单。带上 result_id 时，顺便告诉你这份数据能不能用、
            # 各角色识别到了哪个真实列名。
            tpls = report_templates()
            rid = b.get("result_id")
            if rid:
                res = get_result(rid)
                if res:
                    for t in tpls:
                        t["check"] = report_check(res["columns"], t["id"])
            return self._json({"ok": True, "templates": tpls,
                               "reports": report_list()})

        if p == "/api/report/generate":
            t0 = time.time()
            r = report_generate(b.get("result_id"), b.get("template_id"),
                                b.get("opts") or {}, b.get("name") or "")
            r["elapsed"] = round(time.time() - t0, 2)
            r["ok"] = True
            return self._json(r)

        if p == "/api/report/open":
            fp = open_report(b.get("file") or "")
            return self._json({"ok": True, "path": fp})

        if p == "/api/report/delete":
            fn = os.path.basename(str(b.get("file") or "").strip())
            fp = os.path.abspath(os.path.join(REPORT_DIR, fn))
            if not fn or os.path.dirname(fp) != os.path.abspath(REPORT_DIR):
                raise ValueError("文件名不合法")
            if not os.path.isfile(fp):
                raise ValueError("找不到这份报告：%s" % fn)
            os.remove(fp)
            return self._json({"ok": True, "removed": fn})

        if p == "/api/open_folder":
            fp = b.get("path") or OUT_DIR
            if not os.path.isdir(fp):
                fp = os.path.dirname(fp)
            try:
                os.startfile(fp)  # noqa
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})

        if p == "/api/ai/test":
            txt = ai_chat([{"role": "user", "content": "回复两个字：正常"}], timeout=60)
            return self._json({"ok": True, "reply": txt[:200]})

        if p == "/api/ai/plan":
            q = b.get("question") or ""
            plan, raw = ai_plan(q)
            return self._json({"ok": True, "plan": plan, "raw": raw})

        if p == "/api/ai/analyze":
            txt = ai_analyze(b.get("result_id"), b.get("question") or "请分析这份汇总数据")
            return self._json({"ok": True, "answer": txt})

        if p == "/api/folder_files":
            pth = (b.get("path") or "").strip().strip('"')
            if not os.path.isdir(pth):
                raise ValueError("文件夹不存在：%s" % pth)
            files = []
            for root, _d, fs in os.walk(pth):
                for fn in fs:
                    if fn.startswith("~$") or fn.startswith("."):
                        continue
                    if os.path.splitext(fn)[1].lower() in (".xlsx", ".xlsm", ".csv"):
                        full = os.path.join(root, fn)
                        files.append({"path": full, "name": fn,
                                      "size": os.path.getsize(full),
                                      "rel": os.path.relpath(full, pth)})
            files.sort(key=lambda x: x["rel"])
            return self._json({"ok": True, "files": files})

        # ---------------- 钉钉在线文档 ----------------
        if p == "/api/dingtalk/test":
            """测试钉钉本机 MCP 连接，并列出它提供的工具"""
            init = dingtalk_rpc("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "smartlab-hub", "version": APP_VERSION},
            })
            tools = dingtalk_rpc("tools/list", {})
            names = []
            for t in ((tools.get("result") or {}).get("tools") or []):
                names.append({"name": t.get("name"), "desc": (t.get("description") or "")[:120]})
            return self._json({"ok": True, "server": (init.get("result") or {}).get("serverInfo") or {},
                               "tools": names})

        if p == "/api/dingtalk/cli_pull":
            """命令行桥接：调外部命令把在线文档导成本地文件，然后导入"""
            url = (b.get("url") or (STATE["config"]["dingtalk"].get("doc_url")) or "").strip()
            tmp_dir = os.path.join(LIB_DIR, "dingtalk_pull")
            fp = dingtalk_pull_via_cli(url, tmp_dir)
            ds = import_file(fp, display_name=os.path.basename(fp))
            save_state()
            return self._json({"ok": True, "file": os.path.basename(fp),
                               "dataset": ds_summary(ds)})

        if p == "/api/dingtalk/call":
            """
            直连钉钉 MCP 调工具。参数：tool=工具名, args=参数对象
            返回里如果带本地文件路径 / base64，就顺手存下来并导入。
            """
            tool = (b.get("tool") or "").strip()
            args = b.get("args") or {}
            if not tool:
                raise ValueError("请先选择要调用的钉钉工具")
            res = dingtalk_rpc("tools/call", {"name": tool, "arguments": args})
            if res.get("error"):
                raise ValueError("钉钉返回错误：%s" % json.dumps(res["error"], ensure_ascii=False)[:400])
            saved = dingtalk_save_result(res.get("result") or {}, tool)
            out = {"ok": True, "saved": saved, "dataset": None}
            if saved:
                ds = import_file(saved, display_name=os.path.basename(saved))
                save_state()
                out["dataset"] = ds_summary(ds)
            else:
                out["raw"] = json.dumps(res.get("result"), ensure_ascii=False)[:1500]
            return self._json(out)

        # ---------------- v2.2.0 原始预览：编辑 / 删除 ----------------
        # ★ 这组接口默认只改内存（_EDITS），不会碰 library/sources 里的文件。
        #   要拿到编辑结果：导出成新文件走 /api/raw_edit/export，
        #   想覆盖原文件走 v2.3.0 的 /api/raw_edit/save（它会先自动备份）。
        if p == "/api/raw_edit/apply":
            r = raw_edit_apply(b.get("dataset_id"), b.get("sheet") or "", b.get("ops") or [])
            return self._json(r)

        if p == "/api/fields/delete":
            return self._json(delete_fields(b.get("keys") or [], b.get("dataset_ids") or None))

        if p == "/api/tat":
            res = get_result(b.get("result_id") or "")
            if not res:
                raise ValueError("结果不存在或已过期，请先生成或选择一个分析结果")
            return self._json(tat_analyze(
                res["columns"], res["rows"],
                b.get("start") or "", b.get("end") or "", b.get("group") or ""))

        if p == "/api/raw_edit/preview":
            # 提交改动后拿最新预览（不用再拼一堆查询参数）
            return self._json(raw_preview_edit(
                b.get("dataset_id"), b.get("sheet") or "",
                max(0, int(b.get("offset") or 0)),
                int(b.get("size") or 100)))

        if p == "/api/raw_edit/reset":
            # all=true 走界面的「丢弃编辑」：连被删的子表一起放回来
            return self._json(raw_edit_reset(b.get("dataset_id"), b.get("sheet") or "",
                                             bool(b.get("all"))))

        if p == "/api/raw_edit/export":
            r = raw_edit_export(b.get("dataset_id"), b.get("sheet") or "",
                                (b.get("fmt") or "xlsx").lower(), b.get("name") or "")
            return self._json(r)

        if p == "/api/raw_edit/save":
            # ★ v2.3.0：把编辑结果**写回原始文件**。会先自动备份。
            #   backup 默认 true；前端只在用户明确勾掉时才传 false。
            return self._json(raw_edit_save(b.get("dataset_id"), b.get("sheet") or "",
                                            b.get("backup", True) is not False))

        if p == "/api/raw_edit/integrity":
            # 自证"原文件没被动过"：返回源文件的 mtime/大小/原始行数。
            # v2.3.0 起它同时也是"保存成功了吗"的对照 —— 回写后 mtime 必然变大。
            return self._json(_source_integrity(b.get("dataset_id"), b.get("sheet") or ""))

        # ---- 测试脚手架（仅供自动化测试，界面不调用）----
        if p == "/api/raw_edit/save_clone":
            return self._json(raw_save_clone(b.get("dataset_id")))
        if p == "/api/raw_edit/save_verify":
            return self._json(raw_save_verify(b.get("dataset_id")))
        if p == "/api/raw_edit/save_cleanup":
            return self._json(raw_save_cleanup(b.get("dataset_id")))

        return self._json({"ok": False, "error": "unknown api"}, 404)


def pick_port(preferred=8765):
    for p in range(preferred, preferred + 40):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return 0


def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    load_state()
    try:
        before = STATE.get("recognize_version")
        n = refresh_metadata()
        need_save = (before != RECOGNIZE_VERSION) or bool(n)
        if need_save:
            save_state()
        if before != RECOGNIZE_VERSION:
            print("  已按新版识别规则刷新 %d 个数据表的识别结果" % n)
        elif n:
            print("  已刷新 %d 个数据表的结构信息" % n)
    except Exception:
        traceback.print_exc()
    try:
        r = load_persisted_results()
        if r:
            print("  已载入 %d 份历史汇总结果（可直接导出）" % r)
    except Exception:
        traceback.print_exc()
    port = pick_port(int(os.environ.get("SLH_PORT") or 8765))
    url = "http://127.0.0.1:%d/" % port
    print("=" * 62)
    print("  智慧实验室运营管理 - 数据汇总助手")
    print("  （迈克生物集团 · 实验室运营管理部）")
    print("=" * 62)
    print("  程序版本 : v%s" % APP_VERSION)
    print("  程序目录 : %s" % BASE_DIR)
    print("  数据目录 : %s" % LIB_DIR)
    print("  导出目录 : %s" % OUT_DIR)
    print("  访问地址 : %s" % url)
    print("-" * 62)
    print("  已导入数据表：%d 个" % len(STATE["datasets"]))
    print("  关闭本窗口即退出程序。")
    print("=" * 62)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        print("已退出。")


if __name__ == "__main__":
    main()
