from __future__ import annotations

import re
from collections.abc import Iterable

import pandas as pd


_UNNAMED_PATTERN = re.compile(r"^unnamed(?::\s*\d+)?$", re.IGNORECASE)


def _clean_value(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_header(value: object, fallback: str) -> str:
    text = _clean_value(value)
    if not text or _UNNAMED_PATTERN.match(text):
        return fallback
    return text


def normalize_columns(columns: Iterable[object]) -> list[str]:
    normalized: list[str] = []
    used: dict[str, int] = {}

    for index, column in enumerate(columns, start=1):
        if isinstance(column, tuple):
            parts = [
                _clean_value(part)
                for part in column
                if _clean_value(part) and not _UNNAMED_PATTERN.match(_clean_value(part))
            ]
            raw_value = " / ".join(dict.fromkeys(parts))
        else:
            raw_value = column

        name = normalize_header(raw_value, f"未命名字段{index}")
        count = used.get(name, 0) + 1
        used[name] = count
        normalized.append(name if count == 1 else f"{name}_{count}")

    return normalized


def clean_dataframe_columns(dataframe: pd.DataFrame) -> pd.DataFrame:
    result = dataframe.copy()
    result.columns = normalize_columns(result.columns)
    result = result.dropna(axis="columns", how="all")
    return result
