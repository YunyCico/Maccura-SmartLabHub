# -*- coding: utf-8 -*-
"""TAT 环节识别 · 分段口径 · 实验室 TAT 标准 · 超标判定。

★ 为什么单独一个模块：
  引擎（smartlab_engine.py）已经很大，TAT 的"业务规则"（哪些列算哪个环节、
  标准怎么定、怎么算超标）跟"引擎机制"（读表、算汇总）不是一回事，
  放一起会让两边都难维护。

★ 口径（梦云 2026-10-26 定的）：
  一条标本的完整链路 = 采集 → 签收 → 核收 → 报告 → 审核
  · 采集-签收 ＝ 前段运输（样本从采样点到实验室接收）
  · 签收-核收 ＝ 实验室内部：接收后到标本核收
  · 核收-报告 ＝ 核收到结果产出
  · 报告-审核 ＝ 结果产出到审核签发
  · 实验室内 TAT ＝ 签收 → 审核（三段之和，实验室自己的责任范围）
  · ToTAT       ＝ 采集 → 审核（四段之和，含前段运输）
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

# ---------------------------------------------------------------- 环节定义
# ★ 关键词表只有这一份（后端）。前端原先自己写了一份，两边会漂移。
#   顺序 = 优先级：先命中的列胜出。
STAGE_ORDER = ["采集", "签收", "核收", "报告", "审核"]

STAGE_KEYWORDS: dict[str, list[str]] = {
    "采集": ["采样", "采集", "标本采集"],
    "签收": ["签收", "接收", "收样", "送达"],
    "核收": ["核收", "上机", "离心", "编号"],
    "报告": ["报告", "检测完成", "回报", "完成时间", "出结果"],
    "审核": ["审核", "审校", "签发"],
}

# 明确排除的词：否则「审核时间」会被当成「报告时间」，
# 「报告审核时间」会同时命中两个环节。
STAGE_EXCLUDE: dict[str, list[str]] = {
    "采集": ["审核", "报告", "申请", "打印"],
    "签收": ["审核", "报告", "打印", "完成"],
    "核收": ["审核", "报告", "打印", "完成"],
    "报告": ["审核", "打印", "申请"],
    "审核": ["打印"],
}

# 环节 → 分段（相邻两段）
SEGMENT_PAIRS = [
    ("采集-签收", "采集", "签收"),
    ("签收-核收", "签收", "核收"),
    ("核收-报告", "核收", "报告"),
    ("报告-审核", "报告", "审核"),
]

# ---------------------------------------------------------------- 专业 / 分诊
# 实验室 TAT 标准的四个专业（用户口径）
STANDARD_SPECIALTIES = ["生化", "免疫", "血常规", "凝血"]

# 专业关键词：数据里没有「专业」列时，用这些词去匹配分组/项目列
SPECIALTY_KEYWORDS: dict[str, list[str]] = {
    "生化": ["生化", "生化免疫", "肝功", "肾功"],
    "免疫": ["免疫", "发光", "免疫发光"],
    "血常规": ["血常规", "血细胞", "临检血"],
    "凝血": ["凝血", "血液", "血栓"],
}

# 分诊：值里含"急诊"算急诊，其余算平诊
EMERGENCY_KEYWORDS = ["急诊", "急救", "Emergency", "emergency"]

# 平诊/急诊的判定列候选
URGENCY_COL_CANDIDATES = ["就诊类型", "患者类型", "急诊标志", "门诊类型", "类型"]
# 专业判定列候选（优先用现成的中文专业列）
SPECIALTY_COL_CANDIDATES = ["专业", "检验专业", "项目专业", "专业名称"]
# 分组列候选（专业判定用）
GROUP_COL_CANDIDATES = ["检验小组", "检验分组", "检验大组", "组套", "模块", "项目名称", "检测项目名称"]


def _norm(text: Any) -> str:
    return re.sub(r"\s+", "", str(text or "")).strip()


def detect_stages(columns: list[str] | None) -> dict[str, Any]:
    """按列名自动识别 5 个环节各自对应哪一列。

    返回 {"stages": {环节: 列名}, "missing": [环节...], "candidates": {环节: [候选列...]}}
    """
    cols = [str(c) for c in (columns or []) if c is not None and str(c).strip()]
    stages: dict[str, str] = {}
    candidates: dict[str, list[str]] = {}
    used: set[str] = set()
    for stage in STAGE_ORDER:
        kws = STAGE_KEYWORDS[stage]
        exclude = STAGE_EXCLUDE[stage]
        hits: list[str] = []
        for col in cols:
            if col in used:
                continue
            text = _norm(col)
            if any(k in text for k in kws) and not any(x in text for x in exclude):
                hits.append(col)
        candidates[stage] = hits
        if hits:
            stages[stage] = hits[0]
            used.add(hits[0])
    missing = [s for s in STAGE_ORDER if s not in stages]
    return {"stages": stages, "missing": missing, "candidates": candidates}


def pick_best_stage_column(columns: list[str] | None, rows: list[Any] | None,
                           candidates: list[str], sample: int = 200) -> str:
    """同一环节可能命中好几列，挑**实际有数据**的那一列。

    ★ 真实案例：多张表合并出的结果表里，"采样时间"（来自一张没这个值的表，
      全空）和"采集时间"（有值）同时存在。按列名顺序取第一个命中的 →
      选中全空那列 → "采集-签收"这一段算出 0 样本，整段白算。
      所以要比**非空率**，空列直接排除。
    """
    cols = [str(c) for c in (columns or [])]
    cands = [c for c in (candidates or []) if c in cols]
    if not cands:
        return ""
    if len(cands) == 1:
        return cands[0]
    head = list(rows or [])[:max(1, sample)]
    if not head:
        return cands[0]
    best, best_rate = cands[0], -1.0
    for col in cands:
        try:
            index = cols.index(col)
        except ValueError:
            continue
        filled = sum(1 for row in head
                     if index < len(row) and row[index] not in (None, ""))
        rate = filled / float(len(head))
        if rate > best_rate:
            best, best_rate = col, rate
    return best


def detect_column(columns: list[str] | None, candidates: list[str]) -> str:
    """按候选词找一个列（取第一个包含候选词的列名）。"""
    cols = [str(c) for c in (columns or []) if c is not None and str(c).strip()]
    for key in candidates:
        for col in cols:
            if key in _norm(col):
                return col
    return ""


def classify_specialty(value: Any) -> str:
    """把一个分组/项目名归到 四个专业之一（归不上返回空串）。"""
    text = _norm(value)
    if not text:
        return ""
    for spec, kws in SPECIALTY_KEYWORDS.items():
        for kw in kws:
            if kw in text:
                return spec
    return ""


def classify_urgency(value: Any) -> str:
    """平诊 / 急诊。判不出来返回"未分诊"。"""
    text = _norm(value)
    if not text:
        return "未分诊"
    for kw in EMERGENCY_KEYWORDS:
        if kw in text:
            return "急诊"
    return "平诊"


# ---------------------------------------------------------------- 标准配置
def standards_path(lib_dir: str) -> str:
    return os.path.join(lib_dir, "config", "tat_standards.json")


# 出厂默认的 TAT 规定（小时）。梦云 2026-10-27 定的口径：
#   生化 4 / 2，免疫 4 / 2，血常规 0.5 / 0.25，凝血 2 / 1
DEFAULT_LIMITS: dict[str, dict[str, float]] = {
    "生化": {"平诊": 4.0, "急诊": 2.0},
    "免疫": {"平诊": 4.0, "急诊": 2.0},
    "血常规": {"平诊": 0.5, "急诊": 0.25},
    "凝血": {"平诊": 2.0, "急诊": 1.0},
}


def default_standards() -> dict[str, Any]:
    """默认标准：四个专业 × 平诊/急诊按 DEFAULT_LIMITS 给值。"""
    return {
        "specialties": list(STANDARD_SPECIALTIES),
        "limits": {
            spec: {
                "平诊": DEFAULT_LIMITS.get(spec, {}).get("平诊"),
                "急诊": DEFAULT_LIMITS.get(spec, {}).get("急诊"),
                "单位": "小时",
            }
            for spec in STANDARD_SPECIALTIES
        },
        "target": "lab_tat",     # lab_tat=实验室内TAT | tot=ToTAT
        "updated_at": "",
    }


def load_standards(lib_dir: str) -> dict[str, Any]:
    """读 TAT 标准；文件不存在或坏了就返回默认（不崩）。"""
    data = default_standards()
    path = standards_path(lib_dir)
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                data.update(saved)
    except Exception:
        pass
    limits = data.get("limits")
    if not isinstance(limits, dict):
        limits = {}
    clean: dict[str, Any] = {}
    for spec in STANDARD_SPECIALTIES:
        item = limits.get(spec) or {}
        clean[spec] = {
            "平诊": _as_number(item.get("平诊")),
            "急诊": _as_number(item.get("急诊")),
            "单位": item.get("单位") or "小时",
        }
    data["limits"] = clean
    data["specialties"] = list(STANDARD_SPECIALTIES)
    if data.get("target") not in ("lab_tat", "tot"):
        data["target"] = "lab_tat"
    return data


def _as_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def save_standards(lib_dir: str, payload: dict[str, Any]) -> dict[str, Any]:
    """保存 TAT 标准（只认四个专业 × 平诊/急诊两档）。"""
    from datetime import datetime

    data = default_standards()
    limits_in = payload.get("limits") if isinstance(payload, dict) else None
    limits: dict[str, Any] = {}
    for spec in STANDARD_SPECIALTIES:
        item = (limits_in or {}).get(spec) or {}
        limits[spec] = {
            "平诊": _as_number(item.get("平诊")),
            "急诊": _as_number(item.get("急诊")),
            "单位": item.get("单位") or "小时",
        }
    data["limits"] = limits
    target = payload.get("target") if isinstance(payload, dict) else None
    data["target"] = target if target in ("lab_tat", "tot") else "lab_tat"
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    path = standards_path(lib_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return data


# ---------------------------------------------------------------- 超标判定
def sorted_stats(values: list[float]) -> dict[str, Any]:
    """一组时长（小时）→ 样本数/平均/中位数/P90/最大。"""
    vs = sorted(values)
    n = len(vs)

    def pct(p: float) -> float | None:
        if not n:
            return None
        k = min(n - 1, max(0, int(round(p * n)) - 1))
        return vs[k]

    return {
        "n": n,
        "avg_h": round(sum(vs) / n, 2) if n else None,
        "median_h": round(pct(0.5), 2) if pct(0.5) is not None else None,
        "p90_h": round(pct(0.9), 2) if pct(0.9) is not None else None,
        "max_h": round(vs[-1], 2) if n else None,
    }


def evaluate(rows: list[dict[str, Any]], standards: dict[str, Any],
              target: str = "lab_tat") -> dict[str, Any]:
    """按「专业 × 平诊/急诊」判定超标。

    rows 里每项需含：specialty（专业）、urgency（平诊/急诊）、
    value（被判定的小时数，None 表示该行没算出来）。
    返回：每个专业×分诊的样本数/超标数/超标率，以及未命中标准的部分。
    """
    buckets: dict[tuple[str, str], list[float]] = {}
    for row in rows or []:
        value = row.get("value")
        if value is None:
            continue
        key = (str(row.get("specialty") or ""), str(row.get("urgency") or "未分诊"))
        buckets.setdefault(key, []).append(float(value))
    return evaluate_buckets(buckets, standards, target)


def evaluate_buckets(buckets: dict[tuple[str, str], list[float]],
                     standards: dict[str, Any],
                     target: str = "lab_tat") -> dict[str, Any]:
    """按桶判定超标（引擎内部用：一次遍历就把值分好桶，不必留几十万行明细）。

    buckets 形如 {(专业, 平诊/急诊): [时长(小时), ...]}。
    """
    limits = (standards or {}).get("limits") or {}
    groups = []
    over_all = 0
    judged_all = 0
    for spec in STANDARD_SPECIALTIES:
        for urg in ("平诊", "急诊"):
            values = buckets.get((spec, urg), [])
            stats = sorted_stats(values)
            limit = (limits.get(spec) or {}).get(urg)
            over = 0
            if limit is not None and values:
                over = sum(1 for v in values if v > limit)
            judged = len(values) if limit is not None else 0
            over_all += over
            judged_all += judged
            groups.append({
                "specialty": spec, "urgency": urg,
                "limit_h": limit,
                "over": over,
                "over_pct": round(over * 100.0 / judged, 1) if judged else None,
                "judged": judged,
                "unlimited": len(values) if limit is None else 0,
                **stats,
            })
    # 未匹配到任何专业的行，单独列出来（避免静默丢失）
    other = []
    for (spec, urg), values in buckets.items():
        if spec in STANDARD_SPECIALTIES:
            continue
        other.append({"specialty": spec or "未识别专业", "urgency": urg,
                      **sorted_stats(values)})
    other.sort(key=lambda x: -x["n"])
    return {
        "target": target,
        "groups": groups,
        "other": other,
        "summary": {
            "judged": judged_all,
            "over": over_all,
            "over_pct": round(over_all * 100.0 / judged_all, 1) if judged_all else None,
        },
    }
