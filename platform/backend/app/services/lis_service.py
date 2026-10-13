from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import csv
import hashlib
import io
import json
import os
import re
import uuid

import openpyxl
import pandas as pd

from modules.data_analysis import table_format


PROJECT_ROOT = Path(__file__).resolve().parents[4]
TEMPLATE_SOURCE = Path(__file__).resolve().parents[1] / "resources" / "LIS数据标准模板_中文列名.xlsx"
# ★ LIS 数据归属数据分析模块：modules/data_analysis/data/lis
#   SLH_LIS_DIR 仅供 pytest 隔离使用（见 tests/conftest.py），
#   正常运行时该变量不存在，LIS_DIR 仍指向模块自己的 data/lis。
_LIS_HOME = os.environ.get("SLH_LIS_DIR") or ""
if _LIS_HOME:
    LIS_DIR = Path(_LIS_HOME) / "lis"
else:
    LIS_DIR = PROJECT_ROOT / "modules" / "data_analysis" / "data" / "lis"
STAGING_DIR = LIS_DIR / "staging"
OUTPUT_DIR = LIS_DIR / "standardized"
CONFIG_DIR = LIS_DIR / "mappings"
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
PREVIEW_ROWS = 30
REQUIRED_KEYS = {"record_id", "barcode", "patient_name", "patient_sex", "test_name", "result_text", "received_at", "reported_at"}


def _field_definitions() -> list[dict[str, str]]:
    if not TEMPLATE_SOURCE.is_file():
        raise FileNotFoundError(f"找不到内嵌 LIS 标准模板：{TEMPLATE_SOURCE}")
    wb = openpyxl.load_workbook(TEMPLATE_SOURCE, read_only=True, data_only=True)
    try:
        ws = wb["field_dict"]
        rows = list(ws.iter_rows(min_row=2, values_only=True))
    finally:
        wb.close()
    return [
        {"label": str(row[0]), "key": str(row[1]), "group": str(row[2] or ""), "required": str(row[3]) == "是", "type": str(row[4] or "文本")}
        for row in rows if row and row[0] and row[1]
    ]


def template() -> dict:
    fields = _field_definitions()
    return {"version": "1.1", "fields": fields, "required_keys": sorted(REQUIRED_KEYS)}


def _decode_csv(content: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("CSV 编码无法识别，请另存为 UTF-8 或 GB18030")


def _decode_html(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _html_table_rows(content: bytes) -> list[list[object]]:
    """解析 Excel「导出为网页」产生的 HTML 表格（LIS 导出常见形式）。

    先用标准库解析（零额外依赖）；标准库处理不了的复杂表格再退回 pandas+lxml。
    """
    text = _decode_html(content)
    try:
        rows = _html_rows_with_stdlib(text)
    except ValueError:
        rows = []
    if rows:
        return rows

    from io import StringIO

    try:
        tables = pd.read_html(StringIO(text))
    except ImportError as error:  # pragma: no cover - 依赖缺失时的兜底说明
        raise ValueError("解析 HTML 表格需要 lxml，请安装 lxml==5.3.0") from error
    if not tables:
        raise ValueError("HTML 文件里没有找到表格")
    raw = max(tables, key=lambda frame: frame.shape[0]).where(lambda f: f.notna(), None)
    return [[None if pd.isna(value) else value for value in row] for row in raw.values.tolist()]


def _html_rows_with_stdlib(text: str) -> list[list[object]]:
    """用 html.parser 抓第一个 <table> 的行，不引入额外依赖。"""
    import re
    from html.parser import HTMLParser

    class _TableParser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.rows: list[list[object]] = []
            self._depth = 0
            self._row: list[object] | None = None
            self._cell: list[str] | None = None

        def handle_starttag(self, tag, attrs):
            if tag == "table":
                self._depth += 1
            elif self._depth == 1 and tag == "tr":
                self._row = []
            elif self._depth == 1 and tag in ("td", "th"):
                self._cell = []

        def handle_endtag(self, tag):
            if tag == "table" and self._depth:
                self._depth -= 1
            elif self._depth == 1 and tag == "tr" and self._row is not None:
                self.rows.append(self._row)
                self._row = None
            elif self._depth == 1 and tag in ("td", "th") and self._cell is not None:
                if self._row is None:
                    self._row = []
                self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
                self._cell = None

        def handle_data(self, data):
            if self._cell is not None:
                self._cell.append(data)

    parser = _TableParser()
    parser.feed(text)
    return [row for row in parser.rows if any(str(value).strip() for value in row)]


def _read_workbook(content: bytes, filename: str) -> tuple[list[str], list[list[object]], str, int]:
    suffix = Path(filename).suffix.lower()
    if suffix not in {".xls", ".xlsx", ".xlsm", ".csv", ".txt"}:
        raise ValueError("当前支持 .xls、.xlsx、.xlsm、.csv")

    # ★ 一律按内容分派：LIS 系统导出时经常把 CSV 或 HTML 存成 .xls（实测有 3 份），
    #   只看扩展名会把这些文件全判成"不是有效的 .xls"。
    fmt = table_format.sniff_bytes(content[:8192]) if content else table_format.sniff_extension(suffix)
    if fmt == table_format.FMT_UNKNOWN:
        fmt = table_format.sniff_extension(suffix)

    if fmt in (table_format.FMT_CSV, table_format.FMT_TEXT):
        rows = list(csv.reader(io.StringIO(_decode_csv(content))))
        if not rows:
            raise ValueError("上传文件没有内容")
        return _choose_header(rows)
    if fmt == table_format.FMT_HTML:
        rows = _html_table_rows(content)
        if not rows:
            raise ValueError("HTML 文件里没有可识别的数据表")
        return _choose_header(rows)
    if fmt == table_format.FMT_OLE_XLS:
        try:
            workbook = pd.ExcelFile(io.BytesIO(content), engine="xlrd")
            candidates = []
            for sheet_name in workbook.sheet_names:
                raw = pd.read_excel(workbook, sheet_name=sheet_name, header=None, dtype=object)
                rows = raw.where(raw.notna(), None).values.tolist()
                if not rows:
                    continue
                header, body, _, header_row = _choose_header(rows)
                candidates.append((len([x for x in header if x]), len(body), header, body, sheet_name, header_row))
            if not candidates:
                raise ValueError("工作簿中没有可识别的数据表")
            _, _, header, body, sheet, header_row = max(candidates, key=lambda item: (item[0], item[1]))
            return header, body, sheet, header_row
        except ImportError as error:
            raise ValueError("读取 .xls 需要安装 xlrd==2.0.1") from error
        except ValueError:
            raise
        except Exception as error:
            raise ValueError("旧版 Excel .xls 文件已损坏，无法读取：%s" % error) from error
    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as error:
        raise ValueError("文件不是有效的 Excel Open XML 工作簿，或扩展名与文件内容不匹配") from error
    try:
        candidates = []
        for ws in wb.worksheets:
            raw = list(ws.iter_rows(values_only=True))
            if not raw:
                continue
            header, body, _, header_row = _choose_header([list(row) for row in raw])
            candidates.append((len([x for x in header if x]), len(body), header, body, ws.title, header_row))
        if not candidates:
            raise ValueError("工作簿中没有可识别的数据表")
        _, _, header, body, sheet, header_row = max(candidates, key=lambda item: (item[0], item[1]))
        return header, body, sheet, header_row
    finally:
        wb.close()


def _read_sheet_rows(content: bytes, filename: str, sheet: str) -> list[list[object]]:
    """按"重新指定表头行"重读整表；同样按内容分派，与 _read_workbook 保持一致。"""
    suffix = Path(filename).suffix.lower()
    fmt = table_format.sniff_bytes(content[:8192]) if content else table_format.sniff_extension(suffix)
    if fmt == table_format.FMT_UNKNOWN:
        fmt = table_format.sniff_extension(suffix)
    if fmt in (table_format.FMT_CSV, table_format.FMT_TEXT):
        return list(csv.reader(io.StringIO(_decode_csv(content))))
    if fmt == table_format.FMT_HTML:
        return _html_table_rows(content)
    if fmt == table_format.FMT_OLE_XLS:
        workbook = pd.ExcelFile(io.BytesIO(content), engine="xlrd")
        raw = pd.read_excel(workbook, sheet_name=sheet, header=None, dtype=object)
        return raw.where(raw.notna(), None).values.tolist()
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    try:
        return [list(row) for row in wb[sheet].iter_rows(values_only=True)]
    finally:
        wb.close()


def _choose_header(rows: list[list[object]]) -> tuple[list[str], list[list[object]], str, int]:
    limit = min(len(rows), 30)
    ranked = []
    for idx in range(limit):
        values = [str(v).strip() if v is not None else "" for v in rows[idx]]
        nonempty = [v for v in values if v]
        if not nonempty:
            continue
        unique = len(set(nonempty))
        score = len(nonempty) + unique * 0.3 + (3 if any(k in " ".join(nonempty).lower() for k in ("条码", "姓名", "项目", "结果", "时间")) else 0)
        ranked.append((score, idx, values))
    if not ranked:
        raise ValueError("前 30 行内未找到有效表头")
    _, idx, header = max(ranked)
    header = [h or f"未命名列{n + 1}" for n, h in enumerate(header)]
    body = [list(row) for row in rows[idx + 1:] if any(value not in (None, "") for value in row)]
    return header, body, "CSV", idx + 1


def _suggest_mappings(headers: list[str], fields: list[dict[str, str]]) -> list[dict]:
    synonyms = {
        "record_id": ["记录唯一号", "记录ID"], "barcode": ["条码号", "条码", "标本条码", "标本条码号"],
        "patient_name": ["姓名", "病人姓名", "患者姓名"], "patient_sex": ["性别", "患者性别"],
        "patient_age": ["年龄"], "patient_id": ["病人号", "患者ID", "病历号", "His病历号"],
        "dept_name": ["科室", "申请科室"], "specimen_type": ["标本类型", "样本类型", "标本种类"],
        "test_name": ["检验项目", "项目名称", "送检项目", "申请项目", "检验目的", "检测项目名称"],
        "test_code": ["检验项目编号", "项目编码"], "result_text": ["检验结果", "结果", "结果文本"],
        "result_value": ["定量结果", "结果数值"], "unit": ["单位", "结果单位"],
        "ref_range": ["参考范围", "参考区间"], "abnormal_flag": ["结果状态Code", "异常标记"],
        "ordered_at": ["申请时间", "开立时间"], "collected_at": ["采样时间", "采集时间"],
        "received_at": ["接收时间", "签收时间", "核收时间", "检验科接收时间"],
        "verified_at": ["审核时间"], "reported_at": ["报告时间", "发布时间"], "tat_minutes": ["TAT", "接收-报告(分钟)", "检验中TAT(分钟)"],
        "instrument": ["检验仪器", "报告单元", "仪器"], "test_group": ["检验分组", "检验小组"],
    }
    field_by_key = {f["key"]: f for f in fields}
    result = []
    used: set[str] = set()
    for source in headers:
        normalized = re.sub(r"[\s_（）()【】\[\]：:·-]", "", source).casefold()
        candidates = []
        for field in fields:
            names = [field["label"], field["key"], *synonyms.get(field["key"], [])]
            score = max((1.0 if normalized == re.sub(r"[\s_（）()【】\[\]：:·-]", "", name).casefold() else 0.0 for name in names), default=0)
            if score:
                candidates.append(field)
        chosen = next((f for f in candidates if f["key"] not in used), None)
        if chosen:
            used.add(chosen["key"])
        result.append({"source": source, "target_key": chosen["key"] if chosen else "", "target_label": chosen["label"] if chosen else "", "rule": "自动匹配" if chosen else "不导入"})
    return result


def preview_upload(filename: str, content: bytes, header_row: int | None = None) -> dict:
    if not content:
        raise ValueError("上传文件为空")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("文件超过 100MB 限制")
    headers, body, sheet, detected_header = _read_workbook(content, filename)
    if header_row and header_row != detected_header:
        rows = _read_sheet_rows(content, filename, sheet)
        if not 1 <= header_row <= min(len(rows), 100):
            raise ValueError("表头行号超出范围")
        headers = [str(v).strip() if v is not None else "" for v in rows[header_row - 1]]
        headers = [h or f"未命名列{n + 1}" for n, h in enumerate(headers)]
        body = [list(row) for row in rows[header_row:] if any(value not in (None, "") for value in row)]
        detected_header = header_row
    fields = _field_definitions()
    mapping = _suggest_mappings(headers, fields)
    session_id = uuid.uuid4().hex
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    staged_name = f"{session_id}{Path(filename).suffix.lower()}"
    staged_path = STAGING_DIR / staged_name
    staged_path.write_bytes(content)
    rows = [[_plain(value) for value in row] for row in body]
    metadata_path = STAGING_DIR / f"{session_id}.json"
    metadata_path.write_text(json.dumps({"filename": Path(filename).name, "sheet": sheet, "header_row": detected_header}, ensure_ascii=False), encoding="utf-8")
    return {
        "session_id": session_id, "filename": Path(filename).name, "sheet": sheet,
        "header_row": detected_header, "headers": headers, "row_count": len(rows),
        "mapping": mapping, "fields": fields,
        "preview": rows[:PREVIEW_ROWS], "staged_name": staged_name,
        "format_note": _format_note(content, filename),
    }


def _format_note(content: bytes, filename: str) -> str:
    """把"实际识别到的格式"讲给用户听，避免扩展名与内容不符时用户以为读错了。"""
    suffix = Path(filename).suffix.lower()
    fmt = table_format.sniff_bytes(content[:8192]) if content else table_format.sniff_extension(suffix)
    if fmt == table_format.FMT_UNKNOWN:
        fmt = table_format.sniff_extension(suffix)
    if fmt in {table_format.FMT_OLE_XLS, table_format.FMT_OPENXML} and suffix in {
            ".xls", ".xlsx", ".xlsm"}:
        return ""
    return table_format.describe_mismatch(suffix, fmt)


def _plain(value: object) -> object:
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    return value


def _normalize(value: object, field: dict[str, str]) -> str | float | int | None:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    kind = field["type"]
    if kind == "日期时间":
        if isinstance(value, (datetime, date)):
            dt = datetime.combine(value, datetime.min.time()) if isinstance(value, date) and not isinstance(value, datetime) else value
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y%m%d%H%M%S", "%Y%m%d", "%y.%m.%d"):
            try:
                return datetime.strptime(text, fmt).strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass
        return text
    if kind == "数值":
        match = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
        if not match:
            return None
        number = float(match.group())
        if number < 0:
            return None
        return int(number) if number.is_integer() else number
    if field["key"] == "patient_sex":
        return {"男": "男", "男性": "男", "M": "男", "m": "男", "女": "女", "女性": "女", "F": "女", "f": "女"}.get(text, text)
    if field["key"] == "patient_age_years":
        match = re.search(r"\d+(?:\.\d+)?", text)
        return float(match.group()) if match else None
    return text


def _build_standard(rows: list[list[object]], headers: list[str], mapping: list[dict], fields: list[dict], filename: str, sheet: str, header_row: int, batch_id: str) -> tuple[list[list], list[dict]]:
    indexes = {name: idx for idx, name in enumerate(headers)}
    field_by_key = {field["key"]: field for field in fields}
    out_rows = []
    issues = []
    for row_number, source_row in enumerate(rows, start=header_row + 1):
        record = {field["key"]: None for field in fields}
        for item in mapping:
            target = item.get("target_key", "")
            source = item.get("source", "")
            if not target:
                continue
            if target not in field_by_key or source not in indexes:
                raise ValueError("字段映射包含无效的来源列或标准字段")
            record[target] = _normalize(source_row[indexes[source]] if indexes[source] < len(source_row) else None, field_by_key[target])
        record["record_id"] = record.get("record_id") or f"{batch_id}-{row_number}"
        record["source_file"] = filename
        record["source_sheet"] = sheet
        record["source_header_row"] = header_row
        record["source_row_number"] = row_number
        record["import_batch_id"] = batch_id
        missing = [field_by_key[key]["label"] for key in REQUIRED_KEYS if not record.get(key)]
        if missing:
            issues.append({"row": row_number, "missing": missing})
        out_rows.append([record.get(field["key"]) for field in fields])
    return out_rows, issues


def confirm_save(session_id: str, mapping: list[dict], header_row: int, lab: str = "") -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", session_id or ""):
        raise ValueError("导入会话无效")
    candidates = list(STAGING_DIR.glob(f"{session_id}.*"))
    staged_path = next((path for path in candidates if path.suffix.lower() in {".xls", ".xlsx", ".xlsm", ".csv"}), None)
    metadata_path = STAGING_DIR / f"{session_id}.json"
    if staged_path is None or not staged_path.is_file() or not metadata_path.is_file():
        raise ValueError("暂存文件不存在或已过期，请重新上传")
    content = staged_path.read_bytes()
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    filename = metadata["filename"]
    sheet = metadata["sheet"]
    stored_header_row = int(metadata["header_row"])
    headers, rows, sheet, detected = _read_workbook(content, staged_path.name)
    if header_row != stored_header_row:
        raw_rows = _read_sheet_rows(content, staged_path.name, sheet)
        if not 1 <= header_row <= min(len(raw_rows), 100):
            raise ValueError("表头行号超出范围")
        headers = [str(value).strip() if value is not None else "" for value in raw_rows[header_row - 1]]
        headers = [value or f"未命名列{index + 1}" for index, value in enumerate(headers)]
        rows = [list(row) for row in raw_rows[header_row:] if any(value not in (None, "") for value in row)]
        detected = header_row
    for item in mapping:
        if item.get("target_key") and item["target_key"] not in {f["key"] for f in _field_definitions()}:
            raise ValueError("映射含未知标准字段")
    fields = _field_definitions()
    keys_seen: set[str] = set()
    sources_seen: set[str] = set()
    for item in mapping:
        target = item.get("target_key", "")
        source = item.get("source", "")
        if source not in headers:
            raise ValueError(f"来源字段不存在：{source}")
        if target and target not in {f["key"] for f in fields}:
            raise ValueError(f"标准字段不存在：{target}")
        if target and target in keys_seen:
            raise ValueError(f"标准字段重复映射：{target}")
        if source in sources_seen:
            raise ValueError(f"来源字段重复映射：{source}")
        if target:
            keys_seen.add(target)
        sources_seen.add(source)
    source_filename = filename
    batch_id = uuid.uuid4().hex[:12]
    normalized, issues = _build_standard(rows, headers, mapping, fields, source_filename, sheet, header_row, batch_id)
    missing_required_mapping = [f["label"] for f in fields if f["key"] in REQUIRED_KEYS - {"record_id"} and f["key"] not in keys_seen]
    if missing_required_mapping:
        raise ValueError("以下必填标准字段尚未映射：" + "、".join(missing_required_mapping))
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    output_name = f"LIS标准化_{batch_id}.xlsx"
    output_path = OUTPUT_DIR / output_name
    wb = openpyxl.Workbook(write_only=True)
    ws = wb.create_sheet("lis_std")
    ws.append([field["label"] for field in fields])
    for row in normalized:
        ws.append(row)
    temp_path = output_path.with_suffix(".xlsx.tmp")
    wb.save(temp_path)
    temp_path.replace(output_path)
    mapping_record = {
        "batch_id": batch_id, "source_filename": source_filename, "source_sha256": hashlib.sha256(content).hexdigest(),
        "sheet": sheet, "header_row": header_row, "lab": lab, "mapping": mapping,
        "row_count": len(normalized), "quality_issue_count": len(issues), "created_at": datetime.now().isoformat(timespec="seconds"),
        "output_file": output_name,
    }
    (CONFIG_DIR / f"{batch_id}.json").write_text(json.dumps(mapping_record, ensure_ascii=False, indent=2), encoding="utf-8")
    staged_path.unlink(missing_ok=True)
    return {"batch_id": batch_id, "file": output_name, "rows": len(normalized), "quality_issue_count": len(issues), "quality_issues": issues[:100]}


def output_file(batch_id: str) -> tuple[Path, str]:
    if not re.fullmatch(r"[0-9a-f]{12}", batch_id or ""):
        raise ValueError("批次号无效")
    metadata_path = CONFIG_DIR / f"{batch_id}.json"
    if not metadata_path.is_file():
        raise FileNotFoundError("找不到该 LIS 标准化批次")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    name = Path(metadata.get("output_file", "")).name
    path = OUTPUT_DIR / name
    if not path.is_file() or path.parent.resolve() != OUTPUT_DIR.resolve():
        raise FileNotFoundError("标准化文件不存在")
    return path, name
