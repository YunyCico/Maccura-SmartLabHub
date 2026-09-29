from __future__ import annotations

import csv
import re
from io import BytesIO, StringIO
from typing import Any

import datetime as dt

import pandas as pd

from .field_detector import detect_business_fields, detect_fields, required_field_warnings
from .header_normalizer import normalize_columns


def _text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _raw_rows(raw: pd.DataFrame) -> list[list[str]]:
    return [[_text(value) for value in row] for row in raw.itertuples(index=False, name=None)]


def _is_info_table(rows: list[list[str]]) -> bool:
    pairs = 0
    for row in rows:
        values = [value for value in row if value]
        if len(row) >= 2 and row[0] and row[1] and not any(row[2:]) and len(row[0]) <= 40:
            pairs += 1
    return pairs >= 3


def _parse_info_table(rows: list[list[str]]) -> pd.DataFrame:
    records = [[row[0], row[1]] for row in rows if len(row) >= 2 and row[0] and row[1] and not any(row[2:])]
    return pd.DataFrame(records, columns=["项目", "内容"])


def _looks_numeric(value: str) -> bool:
    if not value:
        return False
    try:
        float(value.replace(",", "").rstrip("%"))
        return True
    except ValueError:
        return False


def _looks_date(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    patterns = ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S")
    for pattern in patterns:
        try:
            dt.datetime.strptime(text, pattern)
            return True
        except ValueError:
            continue
    return bool(re.search(r"\d{4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}", text))


def _looks_data_row(row: list[str]) -> bool:
    values = [value for value in row if value]
    if len(values) < 2:
        return False
    numeric_or_date = sum(_looks_numeric(value) or _looks_date(value) for value in values)
    long_values = sum(len(value) > 20 for value in values)
    return numeric_or_date >= max(1, len(values) // 4) or long_values >= 2


def _header_score(rows: list[list[str]], index: int) -> tuple[int, int, int]:
    current = rows[index]
    width = sum(bool(value) for value in current)
    if width < 2:
        return (0, 0, 0)
    following = rows[index + 1 : index + 4]
    data_rows = sum(sum(bool(value) for value in row) >= 2 for row in following)
    text_headers = sum(not _looks_numeric(value) for value in current if value)
    return (data_rows, width, text_headers)


def _looks_group_header_row(row: list[str]) -> bool:
    values = [value for value in row if value]
    if len(values) < 2 or _looks_data_row(row):
        return False

    unique_count = len(set(values))
    short_text_count = sum(1 <= len(value) <= 20 and not _looks_numeric(value) and not _looks_date(value) for value in values)
    return unique_count < len(values) or short_text_count == len(values)


def _find_header_rows(rows: list[list[str]]) -> list[int]:
    candidates = [index for index in range(min(25, len(rows))) if _header_score(rows, index)[0] > 0]
    if not candidates:
        return [0]

    start = max(candidates, key=lambda index: (_header_score(rows, index), -index))
    header_rows = [start]
    if start + 1 < len(rows):
        first_width = sum(bool(value) for value in rows[start])
        second_width = sum(bool(value) for value in rows[start + 1])
        has_repeated_or_group_value = _looks_group_header_row(rows[start + 1])
        if (
            second_width >= max(2, first_width // 2)
            and _header_score(rows, start + 1)[0] > 0
            and has_repeated_or_group_value
        ):
            header_rows.append(start + 1)
    return header_rows


def _build_headers(rows: list[list[str]], header_rows: list[int], width: int) -> list[str]:
    parts_by_column: list[list[str]] = [[] for _ in range(width)]
    for row_index in header_rows:
        row = rows[row_index]
        previous = ""
        for column_index in range(width):
            value = row[column_index] if column_index < len(row) else ""
            if value:
                previous = value
            if previous:
                parts_by_column[column_index].append(previous)

    raw_headers = [" / ".join(dict.fromkeys(parts)) for parts in parts_by_column]
    return normalize_columns(raw_headers)


def _parse_table(rows: list[list[str]]) -> tuple[pd.DataFrame, list[int]]:
    if not rows:
        return pd.DataFrame(), [0]

    header_rows = _find_header_rows(rows)
    width = max(len(row) for row in rows)
    headers = _build_headers(rows, header_rows, width)
    header_signatures = {tuple(row[:width]) for row in (rows[index] for index in header_rows)}
    body: list[list[str]] = []
    start = max(header_rows) + 1

    for row in rows[start:]:
        padded = row + [""] * (width - len(row))
        if not any(padded):
            continue
        if tuple(padded[:width]) in header_signatures:
            continue
        body.append(padded[:width])

    keep = [index for index, header in enumerate(headers) if header or any(row[index] for row in body)]
    dataframe = pd.DataFrame([[row[index] for index in keep] for row in body], columns=[headers[index] for index in keep])
    return dataframe, header_rows


def _read_csv(content: bytes) -> pd.DataFrame:
    text = ""
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        text = content.decode("utf-8", errors="replace")
    rows = list(csv.reader(StringIO(text)))
    dataframe, _ = _parse_table([[str(value) for value in row] for row in rows])
    return dataframe


def _sheet_preview(dataframe: pd.DataFrame, sheet_name: str, header_rows: list[int], kind: str) -> dict[str, object]:
    columns = [str(column) for column in dataframe.columns]
    warnings = required_field_warnings(columns)
    return {
        "name": sheet_name,
        "kind": kind,
        "header_rows": ",".join(str(index + 1) for index in header_rows),
        "rows": int(dataframe.dropna(how="all").shape[0]),
        "columns": columns,
        "detected_fields": detect_fields(columns),
        "detected_business_fields": detect_business_fields(columns),
        "warnings": [f"缺少{warning}" for warning in warnings],
        "preview": dataframe.head(5).fillna("").astype(str).to_dict(orient="records"),
    }


def read_excel_preview(content: bytes, filename: str) -> dict[str, object]:
    extension = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if extension in {"csv", "txt"}:
        dataframe = _read_csv(content)
        sheet = _sheet_preview(dataframe, "CSV", [0], "table")
        return {"filename": filename, "sheet_count": 1, "total_rows": sheet["rows"], "max_columns": len(sheet["columns"]), "sheets": [sheet]}
    if extension not in {"xlsx", "xlsm", "xltx", "xltm"}:
        raise ValueError("仅支持 .xlsx、.xlsm、.xltx、.xltm、.csv 或 .txt 文件")

    workbook = pd.ExcelFile(BytesIO(content))
    sheets: list[dict[str, object]] = []
    for sheet_name in workbook.sheet_names:
        raw = pd.read_excel(workbook, sheet_name=sheet_name, header=None, dtype=object)
        rows = _raw_rows(raw)
        if _is_info_table(rows):
            dataframe, header_rows, kind = _parse_info_table(rows), [0], "info"
        else:
            dataframe, header_rows = _parse_table(rows)
            kind = "table"
        sheets.append(_sheet_preview(dataframe, sheet_name, header_rows, kind))

    return {
        "filename": filename,
        "sheet_count": len(sheets),
        "total_rows": sum(int(sheet["rows"]) for sheet in sheets),
        "max_columns": max((len(sheet["columns"]) for sheet in sheets), default=0),
        "sheets": sheets,
    }
