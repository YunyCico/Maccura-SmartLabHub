# -*- coding: utf-8 -*-
"""
====================================================================
 分析报告模块（v2.1.0）
====================================================================
把汇总结果变成一份**可直接发人的独立 HTML 分析报告** —— 形态对齐
「成都美年设备运行分析」那一套：结论先行 → 流程链 → KPI 卡片 →
逐节证据（每节一句"读图" + ECharts 图）→ 可落地建议 → 口径说明。

设计原则：
  1. **模板驱动**。报告内容不靠自动猜，而是内置几个固定口径的模板，
     用户选模板 + 指数据即可。这样每份报告的口径稳定、可比、可复用。
  2. **只依赖汇总结果**（columns + rows），不直接碰 Excel。
     所以"跨文件合并"这件事由既有的汇总功能负责，本模块不重复实现。
  3. 计算全部在本机完成，不联网。ECharts 走 CDN，离线时报告仍有表和图注。
  4. 数值一律走宽松还原（复用 app._to_num），与数据概览/透视保持同一口径。

本文件被 app.py import，不单独运行。
====================================================================
"""
import datetime
import json
import math
import re
import unicodedata

# ============================================================================
# 一、字段探测：报告模板要"认得出"用户的列
# ============================================================================

# 字段角色关键词表。按顺序匹配，越靠前优先级越高。
# 用关键词而不是硬编码列名 —— 用户的列名常有细微差异
# （"检测完成时间" / "完成时间" / "出结果时间" / "完成时刻"）。
FIELD_HINTS = {
    "time": [
        ("检测完成时间", "完成时间"), ("完成时间", "完成时间"), ("出结果时间", "出结果时间"),
        ("完成时刻", "完成时间"), ("检测时间", "检测时间"), ("上机时间", "上机时间"),
        ("到单时间", "到单时间"), ("时间", "时间"), ("日期", "日期"),
    ],
    "module": [("模块", "模块"), ("仪器", "仪器"), ("设备", "设备"), ("机台", "机台")],
    "project": [("项目名称", "项目名称"), ("检测项目", "检测项目"), ("项目", "项目"),
                ("检验项目", "检验项目")],
    "patient": [("样本号", "样本号"), ("样本条码", "样本条码"), ("条码", "条码"),
                ("病历号", "病历号"), ("门诊号", "门诊号"), ("患者", "患者")],
    "sample_type": [("样本类型", "样本类型"), ("标本类型", "标本类型"), ("样本", "样本")],
    "redo": [("复查结果", "复查结果"), ("复查完成时间", "复查完成时间"),
             ("复查", "复查"), ("复测", "复测")],
    "dept": [("送检科室", "送检科室"), ("申请科室", "申请科室"), ("科室", "科室"),
             ("部门", "部门"), ("临床科室", "临床科室")],
    "doctor": [("送检医生", "送检医生"), ("申请医生", "申请医生"), ("医生", "医生")],
    "lab": [("实验室", "实验室"), ("院区", "院区"), ("医院", "医院"), ("来源", "来源")],
    "date": [("统计日期", "统计日期"), ("业务日期", "业务日期")],
}

# 报告模板需要哪些"角色"。缺一个就判定不可用（并在界面上说缺哪个）。
TEMPLATE_FIELDS = {
    "equip_load": ("time", "module", "project"),
    "speed_chain": ("time", "module", "project"),
}

# ---------------------------------------------------------------------------
# 时段标签：既支持"点时刻"（2026/09/19 10:05:18），也支持"桶标签"（10:00–11:00）
# ---------------------------------------------------------------------------
_SLOT_RANGE_RE = re.compile(
    r"(\d{1,2})\s*(?::\d{2})?\s*[–—\-~至]\s*(\d{1,2})\s*(?::\d{2})?")
_DT_RE = re.compile(
    r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?"
    r"(?:[ T]?\s*(\d{1,2})\s*[:：]\s*(\d{1,2})(?:\s*[:：]\s*(\d{1,2}))?)?")
_HM_RE = re.compile(r"^\s*(\d{1,2})\s*[:：]\s*(\d{1,2})(?:\s*[:：]\s*(\d{1,2}))?\s*$")


def parse_datetime(v):
    """把各种写法的时间文本还原成 (datetime, 是否只有日期没有时刻)。

    认不出来返回 (None, False)。支持：
      2026/09/19 10:05:18 · 2026-09-19 10:05 · 2026年9月19日 10:05
      2026/09/19（只有日期）· 10:05:18（只有时刻）
    """
    if v is None:
        return None, False
    s = unicodedata.normalize("NFKC", str(v)).strip()
    if not s:
        return None, False
    m = _DT_RE.search(s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if m.group(4) is None:
            try:
                return datetime.datetime(y, mo, d), True
            except ValueError:
                return None, False
        hh = int(m.group(4)) % 24
        mi = int(m.group(5) or 0)
        ss = int(m.group(6) or 0)
        try:
            return datetime.datetime(y, mo, d, hh, mi, ss), False
        except ValueError:
            return None, False
    m = _HM_RE.match(s)
    if m:
        hh = int(m.group(1)) % 24
        return datetime.datetime(1900, 1, 1, hh, int(m.group(2) or 0),
                                 int(m.group(3) or 0)), False
    return None, False


def parse_slot(v):
    """把单元格还原成"时段"。

    返回 (kind, key, start_hour, end_hour, sort_key)：
      kind='hour'  点时刻 → 归到 [h, h+1) 这个小时桶
      kind='range' 区间标签（如 10:00–11:00）→ 直接用它自己的起止
      None 表示认不出来
    """
    if v is None:
        return None
    s = unicodedata.normalize("NFKC", str(v)).strip()
    if not s:
        return None
    # 先判"是不是带完整日期" —— 带日期的时间一律走点时刻分支，
    # 免得 "2026/09/19 10:05" 里的 "19 10" 被误当成 19:00–10:00 这种区间。
    m_dt = _DT_RE.search(s)
    if m_dt and (m_dt.group(4) is not None or m_dt.group(0).count("/") >= 2
                 or "年" in m_dt.group(0) or m_dt.group(0).count("-") >= 2):
        dt, dateonly = parse_datetime(s)
        if dateonly or dt is None:
            # 只有日期没有时刻 —— 认不出"时段"，交给上层计入 bad_time。
            # 绝不能当成 00:00–01:00，那会凭空造出一个凌晨的假时段。
            return None
        h = dt.hour
        return ("hour", "%02d:00–%02d:00" % (h, (h + 1) % 24), h, h + 1, (0, h))
    m = _SLOT_RANGE_RE.search(s)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if 0 <= a <= 24 and a < b <= 25:
            return ("range", "%02d:00–%02d:00" % (a, b), a, b, (0, a))
    dt, _dateonly = parse_datetime(s)
    if dt is not None and dt.year != 1900:
        h = dt.hour
        return ("hour", "%02d:00–%02d:00" % (h, (h + 1) % 24), h, h + 1, (0, h))
    m2 = _HM_RE.match(s)
    if m2:
        h = int(m2.group(1)) % 24
        return ("hour", "%02d:00–%02d:00" % (h, (h + 1) % 24), h, h + 1, (0, h))
    return None


# ============================================================================
# 一·五、多天识别（v2.4.0）
# ----------------------------------------------------------------------------
# 一份数据可能横跨好几天。原始的"时段桶"只保留小时（09:00–10:00），
# 不带日期 —— 于是 09/19 的 09:30 和 09/20 的 09:30 会落进同一个桶、
# 两天的量被悄悄相加，队列重建也会跨天连续累加（第 2 天的积压被抹平）。
# 所以这里先给每一行打上"属于哪一天"，再按天把计算隔离。
# ============================================================================

# 天 key 的两种形态：
#   "2026-09-19" → 真实日期，参与时段分析
#   "__NO_DATE__" → 时间列里没有可识别的日期（只有时刻，如 "10:05:18"）
_NO_DATE_KEY = "__NO_DATE__"
_NO_DATE_LABEL = "未标注日期"


def parse_day(v):
    """返回该时间值所属的"天"。

    (day_key, is_dateonly)：
      · ("2026-09-19", False) 带日期的点时刻 → 正常参与时段分析
      · ("2026-09-19", True)  只有日期没有时刻 → 只贡献"每日完成量"，
                                                 不进小时桶（避免造凌晨假时段）
      · ("__NO_DATE__", False) 只有时刻没日期（如 "10:05:18"）→ 所有这类行
                                                 归到同一"未标注日期"组
      · (None, False) 完全认不出来
    """
    if v is None:
        return None, False
    s = unicodedata.normalize("NFKC", str(v)).strip()
    if not s:
        return None, False
    dt, dateonly = parse_datetime(s)
    if dt is None:
        return None, False
    if dt.year == 1900:                     # 只有时刻，没有日期
        return _NO_DATE_KEY, False
    return dt.strftime("%Y-%m-%d"), dateonly


def day_label(key):
    """天 key → 给人看的短标签。"""
    if key == _NO_DATE_KEY:
        return _NO_DATE_LABEL
    return key


def _day_sort_key(key):
    """天的排序键：真实日期按时间，未标注日期排最后。"""
    return (1, []) if key == _NO_DATE_KEY else (0, key)


# ============================================================================
# 二、字段识别
# ============================================================================

def detect_fields(columns):
    """给定列名列表，识别出各"角色"对应的列名。返回 {角色: 列名}。"""
    out = {}
    used = set()
    for role, hints in FIELD_HINTS.items():
        for kw, label in hints:
            hit = None
            for c in columns:
                if c in used:
                    continue
                if kw == c:                    # 完全相等优先
                    hit = c
                    break
            if hit is None:
                for c in columns:
                    if c in used:
                        continue
                    if kw in c:                # 退而求其次：包含
                        hit = c
                        break
            if hit:
                out[role] = hit
                used.add(hit)
                break
    return out


def field_report(columns, template_id):
    """某个模板在当前列下能不能用、缺什么。"""
    need = TEMPLATE_FIELDS.get(template_id, ())
    found = detect_fields(columns)
    role_label = {
        "time": "时间列（检测完成时间/出结果时间）",
        "module": "模块列（M1/M2/M3 等）",
        "project": "项目名称列",
    }
    missing = [role_label.get(r, r) for r in need if r not in found]
    return {
        "ok": not missing,
        "found": {r: found.get(r) for r in need},
        "found_label": {r: found.get(r) or "—" for r in need},
        "missing": missing,
    }


# ============================================================================
# 三、聚合辅助（与 app.py 的 _to_num 口径保持一致，但独立实现避免循环 import）
# ============================================================================

_THOUSANDS_RE = re.compile(r"^-?\d{1,3}(,\d{3})+(\.\d+)?$")


def to_num(v):
    """宽松数字还原：允许千分位逗号与全角数字；带单位/百分号的一律不当数字。"""
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


def fmt(n):
    """整数加千分位；小数保留 2 位。"""
    if n is None:
        return "—"
    try:
        f = float(n)
    except Exception:
        return str(n)
    if abs(f - int(f)) < 1e-9 and abs(f) < 1e15:
        return "{:,}".format(int(f))
    return "{:,.2f}".format(f)


def pct(a, b, nd=1):
    if not b:
        return "0%"
    return ("%%.%df%%" % nd) % (a * 100.0 / b)


def _col(columns, name, rows):
    """按列名取出一列值。列不在就回空列。"""
    if not name or name not in columns:
        return [""] * len(rows)
    i = columns.index(name)
    return [r[i] if i < len(r) else "" for r in rows]


def _counts(seq):
    d = {}
    for v in seq:
        d[v] = d.get(v, 0) + 1
    return d


def _span_label(slot_keys, slot_meta):
    """一串时段 key → "08:00–12:00" 这样的总跨度标签。"""
    if not slot_keys:
        return ""
    a = slot_meta[slot_keys[0]][0]
    b = slot_meta[slot_keys[-1]][1]
    return "%02d:00–%02d:00" % (a, b)


def split_days(raw_t):
    """把时间列切成"每天一行段"。

    返回 (day_order, day_rows, n_dateonly)：
      · day_order   天 key 列表，按时间排序（未标注日期排最后）
      · day_rows    {天: [(行下标, 时段key or None), ...]}，时段 key 只在**这一天内**唯一
      · n_dateonly  只有日期、没有时刻的行数（只进每日完成量，不进小时桶）

    时段 key 仍然是 "09:00–10:00" 这种小时标签，但**只在当天内使用** ——
    不同天各有一套自己的 slot 列表，所以不会再互相叠加。
    """
    day_rows = {}
    n_dateonly = 0
    for i, v in enumerate(raw_t):
        dk, dateonly = parse_day(v)
        if dk is None:
            continue
        if dateonly:
            n_dateonly += 1
            day_rows.setdefault(dk, []).append((i, None))
            continue
        sp = parse_slot(v)
        if sp is None:
            continue
        _k, key, _a, _b, _sk = sp
        day_rows.setdefault(dk, []).append((i, key))
    day_order = sorted(day_rows.keys(), key=_day_sort_key)
    return day_order, day_rows, n_dateonly


def _slot_map(day_rows, day):
    """{行下标: 时段key} —— 一次建好，避免逐行线性查找。"""
    return {i: sk for i, sk in day_rows.get(day, ()) if sk is not None}


def _day_summary(day_order, day_rows, rows):
    """每天的"完成量"概览。带时刻的行算作有明确归桶，纯日期的行也计入总量。"""
    out = []
    for dk in day_order:
        pairs = day_rows.get(dk, [])
        n_slot = len([1 for _i, k in pairs if k is not None])
        n_dateonly = len(pairs) - n_slot
        out.append({
            "day": dk, "label": day_label(dk),
            "total": len(pairs),
            "n_slot": n_slot, "n_dateonly": n_dateonly,
        })
    return out


# ============================================================================
# 四、模板①：设备模块负载与堵塞点
# ============================================================================

def build_equip_load(columns, rows, fields, opts):
    """设备模块负载与堵塞点分析。

    回答三件事：
      ① 活儿什么时候涌进来（到单/完成节奏）—— 堵塞从哪来
      ② 每个模块每个时段被压了多少 —— 谁在满转、谁闲着
      ③ 根因是"项目在模块上的分配不均"还是"真的产能不够"

    v2.4.0 起支持多天：按天把时段桶隔离，逐天算一遍，再叠加成全天汇总。
    单天数据（含"只有时刻没日期"）走完全一样的逻辑，只是天数=1。
    """
    tcol = fields.get("time")
    mcol = fields.get("module")
    pcol = fields.get("project")

    raw_t = _col(columns, tcol, rows)
    mods = [str(v).strip() if v is not None else "" for v in _col(columns, mcol, rows)]
    projs = [str(v).strip() if v is not None else "" for v in _col(columns, pcol, rows)]

    # ---- 逐行解析时段，丢弃解析不出来的 ----
    slot_of = []          # 每行对应的时段 key
    slot_meta = {}        # key -> (start_h, end_h, sort)
    bad_time = 0
    for v in raw_t:
        sp = parse_slot(v)
        if sp is None:
            bad_time += 1
            slot_of.append(None)
            continue
        _kind, key, a, b, sk = sp
        slot_of.append(key)
        slot_meta[key] = (a, b, sk)

    slot_keys = sorted(slot_meta.keys(), key=lambda k: slot_meta[k][2])
    if not slot_keys:
        raise ValueError(
            "这一列（%s）里没有能识别出来的时间。\n\n"
            "报告需要「点时刻」或「时段标签」两种写法之一，例如：\n"
            "  · 2026/09/19 10:05:18（点时刻，自动归到 10:00–11:00）\n"
            "  · 10:00–11:00（时段标签，直接使用）\n\n"
            "当前列名：%s" % (tcol or "（没找到时间列）", tcol or "—"))

    # ---- ② 模块 × 时段 计数矩阵 ----
    mod_list = sorted({m for m in mods if m}, key=_natkey_cn)
    if not mod_list:
        raise ValueError("「%s」列里没有任何模块名，报告无法按模块拆分。" % (mcol or "模块"))

    # ---- 多天：按天隔离，逐天算一遍，再叠加 ----
    day_order, day_rows, n_dateonly = split_days(raw_t)
    day_keys = [k for k in day_order if any(sk is not None
                                            for _i, sk in day_rows.get(k, []))]
    if not day_keys:
        raise ValueError(
            "这一列（%s）里没有能识别出来的时间。\n\n"
            "报告需要「点时刻」或「时段标签」两种写法之一，例如：\n"
            "  · 2026/09/19 10:05:18（点时刻，自动归到 10:00–11:00）\n"
            "  · 10:00–11:00（时段标签，直接使用）\n\n"
            "当前列名：%s" % (tcol or "（没找到时间列）", tcol or "—"))

    multi_day = len(day_keys) > 1 or (n_dateonly > 0 and len(day_keys) >= 1)

    # 每天各自一套时段（key 只在当天内唯一）
    day_slots = {}
    for dk in day_keys:
        meta = {}
        for _i, sk in day_rows.get(dk, []):
            if sk is None:
                continue
            sp = parse_slot(raw_t[_i])
            if sp:
                meta[sk] = (sp[2], sp[3], sp[4])
        day_slots[dk] = (sorted(meta.keys(), key=lambda k: meta[k][2]), meta)

    # 全天（跨天合并）的时段轴：同一小时标签在不同天合并成一条
    slot_meta = {}
    for dk in day_keys:
        for k in day_slots[dk][0]:
            slot_meta.setdefault(k, day_slots[dk][1][k])
    slot_keys = sorted(slot_meta.keys(), key=lambda k: slot_meta[k][2])
    m_idx = {m: i for i, m in enumerate(mod_list)}
    s_idx = {k: i for i, k in enumerate(slot_keys)}

    def _day_grid(dk):
        """某一天的 模块 × 时段 矩阵（时段按当天的 slot 顺序）。"""
        dslots, _dm = day_slots[dk]
        di = {k: i for i, k in enumerate(dslots)}
        g = [[0] * len(dslots) for _ in mod_list]
        for i, sk in day_rows.get(dk, []):
            if sk is None:
                continue
            mi = mods[i] if i < len(mods) else ""
            if not mi:
                continue
            g[m_idx[mi]][di[sk]] += 1
        return g, dslots

    # 全天矩阵：把每天矩阵按小时标签累加
    grid = [[0] * len(slot_keys) for _ in mod_list]
    for dk in day_keys:
        dg, dslots = _day_grid(dk)
        for r in range(len(mod_list)):
            for j, sk in enumerate(dslots):
                grid[r][s_idx[sk]] += dg[r][j]

    # ---- ① 时段总量（到单节奏）----
    slot_total = [0] * len(slot_keys)
    for row in grid:
        for i, v in enumerate(row):
            slot_total[i] += v
    grand = sum(slot_total)

    # 峰值时段
    pk_i = max(range(len(slot_total)), key=lambda i: slot_total[i]) if slot_total else 0
    pk_slot = slot_keys[pk_i] if slot_keys else ""
    pk_val = slot_total[pk_i] if slot_total else 0

    # 前 N 个时段占全天比例（密集度）
    order = sorted(range(len(slot_total)), key=lambda i: -slot_total[i])
    top_n = min(5, len(order))
    top_share = sum(slot_total[i] for i in order[:top_n]) / grand if grand else 0

    # 活跃时段（非零）
    active = [i for i, v in enumerate(slot_total) if v > 0]
    span = (slot_meta[slot_keys[active[-1]]][1] - slot_meta[slot_keys[active[0]]][0]
            if active else 0)

    # ---- 逐日明细：每天各算一份，用于"逐日对比"章节 ----
    days = []
    for dk in day_keys:
        dg, dslots = _day_grid(dk)
        d_slot_total = [0] * len(dslots)
        for row in dg:
            for j, v in enumerate(row):
                d_slot_total[j] += v
        d_grand = sum(d_slot_total)
        d_pk_i = (max(range(len(d_slot_total)), key=lambda i: d_slot_total[i])
                  if d_slot_total else 0)
        d_mod_total = [sum(r) for r in dg]
        d_idle = [v for v in d_mod_total if v > 0]
        d_imbal = (max(d_idle) / min(d_idle)) if len(d_idle) > 1 and min(d_idle) > 0 else None
        days.append({
            "day": dk, "label": day_label(dk),
            "slots": dslots, "grid": dg,
            "slot_total": d_slot_total, "grand": d_grand,
            "peak_slot": dslots[d_pk_i] if dslots else "",
            "peak_val": d_slot_total[d_pk_i] if d_slot_total else 0,
            "mod_total": d_mod_total,
            "imbalance": round(d_imbal, 2) if d_imbal else None,
            "n_dateonly": len([1 for _i, sk in day_rows.get(dk, []) if sk is None]),
            "span_label": _span_label(dslots, day_slots[dk][1]),
            "gap_hours": _find_lull(dslots, day_slots[dk][1], d_slot_total),
        })
    summary_days = _day_summary(day_order, day_rows, rows)

    # ---- 模块负载率（相对最忙模块）----
    mod_total = [sum(r) for r in grid]
    mmax = max(mod_total) if mod_total else 0
    mod_share = [(_s / mmax) if mmax else 0 for _s in mod_total]

    # ---- 模块 × 时段 热力图：负载率 = 该格 / 该时段最大格 ----
    col_max = [max((grid[m][i] for m in range(len(mod_list))), default=0)
               for i in range(len(slot_keys))]
    heat = []
    for m in range(len(mod_list)):
        line = []
        for i in range(len(slot_keys)):
            cm = col_max[i]
            line.append(round(grid[m][i] / cm, 3) if cm else 0)
        heat.append(line)

    # ---- ⑤ TOP10 项目 ----
    pc = _counts([p for p in projs if p])
    top_proj = sorted(pc.items(), key=lambda kv: -kv[1])[:10]

    # ---- 模块 × 项目 归属（判"分配结构问题"）----
    mod_proj = {}
    for m, p in zip(mods, projs):
        if not m or not p:
            continue
        mod_proj.setdefault(m, {})
        mod_proj[m][p] = mod_proj[m].get(p, 0) + 1
    # 每个模块的头部项目（占该模块量最大的一批）
    mod_pack = {}
    for m, d in mod_proj.items():
        tot = sum(d.values())
        top = sorted(d.items(), key=lambda kv: -kv[1])[:4]
        mod_pack[m] = {"total": tot,
                       "top": [[k, v, round(v * 100.0 / tot, 1) if tot else 0]
                               for k, v in top]}

    # ---- 堵塞点判定 ----
    # 单点过载：某模块某时段占了该时段全体的 40% 以上，且该时段总量不小
    overload = []
    for m in range(len(mod_list)):
        for i in range(len(slot_keys)):
            if slot_total[i] < 3:
                continue
            r = grid[m][i] / slot_total[i]
            if r >= 0.40:
                overload.append({
                    "module": mod_list[m], "slot": slot_keys[i],
                    "count": grid[m][i], "share": round(r * 100, 1),
                    "slot_total": slot_total[i],
                })
    overload.sort(key=lambda x: -x["share"])
    overload = overload[:8]

    # 负载极差（最忙/最闲模块，用来判"分配不均"）
    idle = [v for v in mod_total if v > 0]
    imbalance = (max(idle) / min(idle)) if len(idle) > 1 and min(idle) > 0 else None

    return {
        "template": "equip_load",
        "columns_used": {"time": tcol, "module": mcol, "project": pcol},
        "row_count": len(rows),
        "bad_time": bad_time,
        # ---- v2.4.0 多天 ----
        "multi_day": multi_day,
        "n_days": len(day_keys),
        "day_keys": day_keys,
        "day_labels": [day_label(k) for k in day_keys],
        "days": days,
        "summary_days": summary_days,
        "n_dateonly": n_dateonly,
        "slots": slot_keys,
        "mod_list": mod_list,
        "grid": grid,
        "heat": heat,
        "slot_total": slot_total,
        "slot_share": [round(v * 100.0 / grand, 1) if grand else 0 for v in slot_total],
        "grand": grand,
        "peak_slot": pk_slot, "peak_val": pk_val,
        "top_share": round(top_share * 100, 1), "top_n": top_n,
        "active_slots": len(active), "span_hours": span,
        "mod_total": mod_total,
        "mod_share_pct": [round(s * 100, 1) for s in mod_share],
        "top_proj": [[k, v, round(v * 100.0 / grand, 1) if grand else 0] for k, v in top_proj],
        "mod_pack": mod_pack,
        "overload": overload,
        "imbalance": round(imbalance, 2) if imbalance else None,
        "gap_hours": _find_lull(slot_keys, slot_meta, slot_total),
    }


def _find_lull(slot_keys, slot_meta, slot_total):
    """找"低谷时段"：连续且总量低于全天均值 40% 的时段。"""
    if not slot_total:
        return {"slots": [], "label": ""}
    avg = sum(slot_total) / len(slot_total)
    lo = [i for i, v in enumerate(slot_total) if v < avg * 0.4]
    if not lo:
        return {"slots": [], "label": ""}
    runs, cur = [], None
    for i in lo:
        if cur is not None and i == cur[-1] + 1:
            cur.append(i)
        else:
            if cur:
                runs.append(cur)
            cur = [i]
    if cur:
        runs.append(cur)
    best = max(runs, key=len)
    a = slot_meta[slot_keys[best[0]]][0]
    b = slot_meta[slot_keys[best[-1]]][1]
    return {"slots": [slot_keys[i] for i in best],
            "label": "%02d:00–%02d:00" % (a, b),
            "total": sum(slot_total[i] for i in best)}


def _natkey_cn(s):
    """自然排序：M1 < M2 < M10；无数字的按文本。"""
    parts = re.split(r"(\d+)", str(s))
    return [(0, int(p)) if p.isdigit() else (1, p) for p in parts if p != ""]


def _src_col(columns):
    """找能区分「数据来自哪份文件/哪台设备」的列。

    汇总时如果开了 add_source_col，会前置「来源实验室 / 来源文件 / 来源工作表」。
    判重名要用**能区分设备**的那一列 —— 「来源实验室」常是全所同一个名字，
    真正区分设备的是「来源文件」（如 0919-2.xlsx / 0919-3.xlsx），
    所以优先取「来源文件」，其次才是其余。
    """
    for c in ("来源文件", "来源工作表", "来源实验室"):
        if c in columns:
            return c
    return None


# 跨设备重名时单元名形如「0919-2.xlsx · M1」，用 " · " 分隔来源与模块名。
_UNIT_SEP = " · "


def _unit_mod(unit):
    """从单元名里取模块名。"""
    s = str(unit)
    return s.rsplit(_UNIT_SEP, 1)[-1] if _UNIT_SEP in s else s


def _unit_src(unit):
    """从单元名里取来源（设备）。单设备时返回「全线」。"""
    s = str(unit)
    return s.rsplit(_UNIT_SEP, 1)[0] if _UNIT_SEP in s else "全线"


# ============================================================================
# 五、模板②：免疫线实际测试速度 · 证据链
# ============================================================================

def _pick_window(util, start_h):
    """在**同一条**队列曲线上找饱和窗。返回 (i0, i1) 或 None。

    取"利用率 ≥ 80% 产能"的最长连续段；没有就退到 ≥60%；再没有返回 None。
    ★ 关键：必须在**单天内部**调用 —— 跨天连起来找会把两天的段拼成一段。
    """
    for thr in (80, 60):
        idx = [i for i, u in enumerate(util) if u >= thr]
        runs, cur = [], None
        for i in idx:
            if cur is not None and i == cur[-1] + 1:
                cur.append(i)
            else:
                if cur:
                    runs.append(cur)
                cur = [i]
        if cur:
            runs.append(cur)
        if runs:
            best = max(runs, key=len)
            return best[0], best[-1]
    return None


def _queue_curve(done, cap):
    """队列重建：队列(t) = 累计完成 − 标称产能 × 已过时段。"""
    cum_d, queue = [], []
    cd = 0
    for i in range(len(done)):
        cd += done[i]
        cum_d.append(cd)
        queue.append(max(0, cd - cap * (i + 1)))
    return cum_d, queue


def _norm_window(win):
    """解析"测速窗口"参数 → (a, b)；auto 返回 None。非法则抛错。"""
    if win == "auto":
        return None
    try:
        a, b = [int(x) for x in win.replace("–", ",").replace("-", ",")
                .replace("~", ",").split(",") if x.strip()]
    except (ValueError, TypeError):
        raise ValueError(
            "「测速窗口」填的不对：%s\n\n"
            "正确写法是两段数字，用逗号分隔，例如 14,20 表示 14:00–20:00；\n"
            "留空或填 auto 则由程序自动识别饱和窗。" % win)
    if not (0 <= a < b <= 24):
        raise ValueError("「测速窗口」%s 不合法：起止应满足 0 ≤ 起 < 止 ≤ 24。" % win)
    return a, b


def build_speed_chain(columns, rows, fields, opts):
    """队列重建 → 饱和窗识别 → 模块实际速度 → 项目包 → 均衡化。

    方法论（与美年那份一致）：
      到达 = 检测完成时间所在的时段（本工具只有一列时间，故到达/离开同源，
             这点必须在报告的"口径说明"里写清楚，不能假装有两列时间）
      队列(t) = 累计完成 − 累计"按标称产能应完成"
      实际速度 = 饱和窗完成数 ÷ 窗口时长

    ★ v2.4.0 多天：队列/积压是「当天从零起算」的量 —— 跨天累加会把前一天的
      积压带进第二天，算出假速度。所以**每天各自重建队列、各自找饱和窗**，
      再取"速度最高的那天"作为能力证据，同时给出每天的对照。
    """
    tcol = fields.get("time")
    mcol = fields.get("module")
    pcol = fields.get("project")

    nominal = int(opts.get("nominal") or 270)      # 单模块标称 测试/h
    win = str(opts.get("window") or "auto")
    ctrl = [s.strip() for s in str(opts.get("control") or "").split(",") if s.strip()]
    # 模块列里的名字未必等于"真机台数"：把 3联机-1 和 3联机-2 合并时，
    # 两台的 M1/M2/M3 会同名，这时 mod_list 只有 3 个名字但实际有 6 个模块。
    # 允许手工指定真机模块数，默认自动判。
    mod_count_opt = opts.get("mod_count")

    raw_t = _col(columns, tcol, rows)
    mods = [str(v).strip() if v is not None else "" for v in _col(columns, mcol, rows)]
    projs = [str(v).strip() if v is not None else "" for v in _col(columns, pcol, rows)]

    bad_time = sum(1 for v in raw_t if parse_slot(v) is None)

    # ---- v2.4.0 多天：按天隔离（时段 key 只在当天内唯一）----
    day_order, day_rows, n_dateonly = split_days(raw_t)
    day_keys = [k for k in day_order if any(sk is not None
                                            for _i, sk in day_rows.get(k, []))]
    if not day_keys:
        raise ValueError("时间列「%s」里没有识别出任何时段，无法做队列重建。"
                         % (tcol or "—"))
    multi_day = len(day_keys) > 1

    # 每天各自的时段轴
    day_slots = {}
    for dk in day_keys:
        meta = {}
        for _i, sk in day_rows.get(dk, []):
            if sk is None:
                continue
            sp = parse_slot(raw_t[_i])
            if sp:
                meta[sk] = (sp[2], sp[3], sp[4])
        day_slots[dk] = (sorted(meta.keys(), key=lambda k: meta[k][2]), meta)

    # 全天（跨天合并）的时段轴
    slot_meta = {}
    for dk in day_keys:
        for k in day_slots[dk][0]:
            slot_meta.setdefault(k, day_slots[dk][1][k])
    slot_keys = sorted(slot_meta.keys(), key=lambda k: slot_meta[k][2])

    mod_list = sorted({m for m in mods if m}, key=_natkey_cn)
    if not mod_list:
        raise ValueError("「%s」列里没有模块名。" % (mcol or "模块"))
    m_idx = {m: i for i, m in enumerate(mod_list)}
    s_idx = {k: i for i, k in enumerate(slot_keys)}

    # 全天矩阵（跨天合并，用于展示层）
    grid = [[0] * len(slot_keys) for _ in mod_list]
    day_grids = {}
    for dk in day_keys:
        dslots, _dm = day_slots[dk]
        di = {k: i for i, k in enumerate(dslots)}
        g = [[0] * len(dslots) for _ in mod_list]
        for i, sk in day_rows.get(dk, []):
            if sk is None:
                continue
            mi = mods[i] if i < len(mods) else ""
            if not mi:
                continue
            g[m_idx[mi]][di[sk]] += 1
        day_grids[dk] = g
        for r in range(len(mod_list)):
            for j, sk in enumerate(dslots):
                grid[r][s_idx[sk]] += g[r][j]

    n = len(slot_keys)
    done = [sum(grid[m][i] for m in range(len(mod_list))) for i in range(n)]

    # ---- 真机模块数 ----
    # 模块列里的名字可能被"跨设备重名"：把两台 3联机 的数据合在一起时，
    # 两台的 M1/M2/M3 会撞名，mod_list 只有 3 个名字但实际有 6 个模块。
    # 产能必须按真机模块数算，否则利用率会虚高、速度会虚高。
    src_col = _src_col(columns)
    distinct_units = 0
    if src_col:
        pairs = set()
        for m, s in zip(mods, _col(columns, src_col, rows)):
            if m and str(s).strip():
                pairs.add((str(s).strip(), m))
        distinct_units = len(pairs)
    auto_count = max(len(mod_list), distinct_units)
    mod_count = auto_count
    if mod_count_opt not in (None, ""):
        try:
            v = int(mod_count_opt)
            if v >= 1:
                mod_count = v
        except (TypeError, ValueError):
            pass          # 填了非法值就当没填，用自动值
    # mod_count 比自动值大、且模块名有重名时，说明用户知道实际机台更多；
    # 比 auto 小则不采信（会算出虚高的利用率），回退到自动值。
    if mod_count < auto_count:
        mod_count = auto_count
    name_dup = distinct_units > len(mod_list)
    cap = nominal * mod_count                   # 全线每小时产能

    win_range = _norm_window(win)               # None = auto

    # ---- 逐天重建队列、逐天找饱和窗（★ 多天的核心修正）----
    # 队列是"当天从零起算"的量：把两天的完成量连起来累加，会把第一天的
    # 积压带进第二天，导致队列曲线失真、饱和窗跨天、速度算错。
    day_wins = {}      # dk -> {w_slots, i0, i1, hours, label, done, cum, queue, util}
    for dk in day_keys:
        dslots = day_slots[dk][0]
        ddone = [sum(day_grids[dk][m][j] for m in range(len(mod_list)))
                 for j in range(len(dslots))]
        d_cum, d_queue = _queue_curve(ddone, cap)
        d_util = [round(ddone[j] * 100.0 / cap, 1) if cap else 0
                  for j in range(len(dslots))]
        d_start_h = [day_slots[dk][1][k][0] for k in dslots]
        if win_range is None:
            pick = _pick_window(d_util, d_start_h)
        else:
            a, b = win_range
            cand = [j for j, h in enumerate(d_start_h) if a <= h < b]
            pick = (cand[0], cand[-1]) if cand else None
        if pick is None:
            continue
        di0, di1 = pick
        day_wins[dk] = {
            "day": dk, "label": day_label(dk),
            "slots": dslots, "i0": di0, "i1": di1,
            "hours": di1 - di0 + 1,
            "label_win": "%02d:00–%02d:00" % (d_start_h[di0],
                                              day_slots[dk][1][dslots[di1]][1]),
            "done": ddone, "cum": d_cum, "queue": d_queue, "util": d_util,
            "peak": max(ddone) if ddone else 0,
            "grand": sum(ddone),
        }
    # 有饱和窗的优先；一天都没找到 → 拿数据最全的一天来报错
    win_days = [dk for dk in day_keys if dk in day_wins]
    if not win_days:
        raise ValueError(
            "所有时段的完成量都远低于标称产能（%d 测试/h×%d 模块）。\n\n"
            "队列法要求至少有一段「被压满」的时间窗口。请核对：\n"
            "  · 标称速度参数是否填得过大（当前 %d）\n"
            "  · 数据是否只是一天里很短的一段\n\n"
            "也可以手工指定窗口（如 14,20）后重试。"
            % (nominal, len(mod_list), nominal))

    # ---- 代表天 = 饱和窗内实际速度最高的那天（能力证据）----
    def _win_speed_day(dk):
        w = day_wins[dk]
        seg = w["done"][w["i0"]:w["i1"] + 1]
        return (sum(seg) / float(len(seg))) if seg else 0
    best_day = max(win_days, key=_win_speed_day)
    rep = day_wins[best_day]
    rep_slots = rep["slots"]

    # 代表天的时段轴作为报告主轴（单天时与从前的 slot_keys 完全相同）
    slot_keys = rep_slots
    slot_meta = day_slots[best_day][1]
    n = len(slot_keys)
    s_idx = {k: i for i, k in enumerate(slot_keys)}
    grid = [[0] * n for _ in mod_list]
    for r in range(len(mod_list)):
        for j, sk in enumerate(rep_slots):
            grid[r][j] = day_grids[best_day][r][j]
    done = rep["done"]
    cum_d, queue, util = rep["cum"], rep["queue"], rep["util"]
    start_h = [slot_meta[k][0] for k in slot_keys]
    i0, i1 = rep["i0"], rep["i1"]

    # 逐日对照表（每天都列，包含没有饱和窗的天）
    days_cmp = []
    for dk in day_keys:
        w = day_wins.get(dk)
        s = None
        for sd in _day_summary(day_keys, day_rows, rows):
            if sd["day"] == dk:
                s = sd
                break
        days_cmp.append({
            "day": dk, "label": day_label(dk),
            "total": s["total"] if s else 0,
            "n_dateonly": s["n_dateonly"] if s else 0,
            "peak": w["peak"] if w else None,
            "peak_slot": (w["slots"][w["done"].index(max(w["done"]))]
                          if w and w["done"] else ""),
            "win_label": w["label_win"] if w else "",
            "win_hours": w["hours"] if w else 0,
            "speed_long": round(_win_speed_day(dk), 1) if w else None,
            "grand": w["grand"] if w else (s["total"] if s else 0),
        })

    hours = i1 - i0 + 1
    win_label = "%02d:00–%02d:00" % (start_h[i0], slot_meta[slot_keys[i1]][1])

    # ---- 逐"真机单元"的完成量 ----
    # unit 列表：正常情况下就是模块名；跨设备重名时用「来源文件 · 模块名」区分，
    # 这样每台设备的每个模块各自算速度，不会把两台的量加到一个名字上。
    unit_list = list(mod_list)
    ugrid = [list(g) for g in grid]
    # 代表天的行下标（多天时不能拿全量 rows 来填 ugrid —— s_idx 只认代表天）
    rep_smap = _slot_map(day_rows, best_day)
    rep_rows = sorted(rep_smap.keys())
    if name_dup and src_col:
        srcs = [str(v).strip() if v is not None else ""
                for v in _col(columns, src_col, rows)]
        pairs = sorted({(srcs[i], mods[i]) for i in rep_rows
                        if srcs[i] and mods[i]})
        unit_list = ["%s · %s" % (s, m) for s, m in pairs]
        u_idx = {p: i for i, p in enumerate(pairs)}
        ugrid = [[0] * len(slot_keys) for _ in pairs]
        for i in rep_rows:
            s, m = srcs[i], mods[i]
            sk = rep_smap.get(i)
            if sk is None or not s or not m:
                continue
            ugrid[u_idx[(s, m)]][s_idx[sk]] += 1

    def win_speed(a, b):
        return [round(sum(ugrid[m][a:b + 1]) / float(b - a + 1), 1)
                for m in range(len(unit_list))]

    w_long = win_speed(i0, i1)
    # 短窗：饱和窗的前半段。取 ceil 保证至少 1 个时段，
    # 这样短窗是长窗的"子集"，速度天然 ≥ 长窗（用于给"下限估计"加一条旁证）。
    half = max(1, (hours + 1) // 2)
    i_mid = i0 + half - 1
    w_short = win_speed(i0, i_mid)
    win_short_label = "%02d:00–%02d:00" % (start_h[i0], slot_meta[slot_keys[i_mid]][1])

    peak = [max(ugrid[m]) for m in range(len(unit_list))]
    peak_slot = [slot_keys[ugrid[m].index(max(ugrid[m]))] for m in range(len(unit_list))]

    # ---- 对照组（不参与速度解读）----
    # 对照组按"模块名"匹配即可：跨设备时两台同名模块都算对照组。
    ctrl_idx = [i for i, k in enumerate(unit_list)
                if _unit_mod(k) in ctrl] if ctrl else []
    ctrl_devices = [k for k in unit_list if _unit_mod(k) in ctrl] if ctrl else []

    # ---- 项目包归属 ----
    mod_proj = {}
    for m, p in zip(mods, projs):
        if not m or not p:
            continue
        mod_proj.setdefault(m, {})
        mod_proj[m][p] = mod_proj[m].get(p, 0) + 1
    all_proj = {}
    for d in mod_proj.values():
        for p, v in d.items():
            all_proj[p] = all_proj.get(p, 0) + v
    core = [p for p, _ in sorted(all_proj.items(), key=lambda kv: -kv[1])[:6]]

    # 每个模块的"项目包"构成（量最大的前 4 项）—— 证据④ 用
    mod_pack = {}
    for m, dd in mod_proj.items():
        tot = sum(dd.values())
        top = sorted(dd.items(), key=lambda kv: -kv[1])[:4]
        mod_pack[m] = {"total": tot,
                       "top": [[k, v, round(v * 100.0 / tot, 1) if tot else 0]
                               for k, v in top]}

    mod_keys = unit_list
    meta = []
    hot_idx = [i for i in range(len(unit_list))
               if i not in ctrl_idx and w_long[i] >= 0.96 * nominal]
    for i, k in enumerate(mod_keys):
        is_ctrl = i in ctrl_idx
        is_hot = i in hot_idx
        d = mod_proj.get(_unit_mod(k), {})
        top = max(d.items(), key=lambda kv: kv[1])[0] if d else "—"
        if is_ctrl:
            judge = "对照组·不作速度解读"
        elif is_hot:
            judge = "满转（%s 项目包）" % top
        else:
            judge = "受分配量限制的表观速度"
        meta.append({"key": k, "ctrl": is_ctrl, "hot": is_hot, "top": top,
                     "judge": judge})

    # ---- 均衡化模拟：同"设备"下的模块，按模块数把量摊平 ----
    # 有来源列就按来源（设备）分组，各台分别摊平；否则当成一条全线。
    bal = {}
    groups = {}
    for k in unit_list:
        groups.setdefault(_unit_src(k), []).append(k)
    if len(groups) <= 1:
        groups = {"全线": list(unit_list)}
    for gname, keys in groups.items():
        idxs = [i for i, k in enumerate(unit_list) if k in keys
                and _unit_mod(k) not in ctrl]
        if not idxs:
            continue
        avg = sum(w_long[i] for i in idxs) / len(idxs)
        bal[gname] = {
            "per_module": round(avg, 1),
            "pct": round(avg / nominal * 100, 1) if nominal else 0,
            "n_modules": len(idxs),
            "cur_pct": [round(w_long[i] / nominal * 100, 1) if nominal else 0
                        for i in idxs],
            "members": [unit_list[i] for i in idxs],
        }

    hot_long = ([w_long[i] for i in hot_idx]
                or [w_long[i] for i, m in enumerate(meta) if not m["ctrl"]]
                or list(w_long))
    hot_short = ([w_short[i] for i in hot_idx]
                 or [w_short[i] for i, m in enumerate(meta) if not m["ctrl"]]
                 or list(w_short))
    hot_peak = ([peak[i] for i in hot_idx]
                or [max(peak) if peak else 0])
    hot_label = "/".join(sorted({_unit_mod(unit_list[i]) for i in hot_idx})) or "瓶颈模块"

    return {
        "template": "speed_chain",
        "columns_used": {"time": tcol, "module": mcol, "project": pcol,
                         "source": src_col},
        "row_count": len(rows), "bad_time": bad_time,
        "nominal": nominal, "mod_count": mod_count,
        "auto_mod_count": auto_count, "name_dup": name_dup,
        "distinct_units": distinct_units,
        # ---- v2.4.0 多天 ----
        "multi_day": multi_day,
        "n_days": len(day_keys),
        "day_keys": day_keys,
        "day_labels": [day_label(k) for k in day_keys],
        "best_day": best_day,
        "best_day_label": day_label(best_day),
        "days_cmp": days_cmp,
        "n_dateonly": n_dateonly,
        "slots": slot_keys, "mod_list": unit_list, "grid": ugrid,
        "done": done, "queue": queue, "util": util, "cumDone": cum_d,
        "cap": cap,
        "win_label": win_label, "win_hours": hours,
        "win_short_label": win_short_label,
        "w_long": w_long, "w_short": w_short,
        "peak": peak, "peak_slot": peak_slot,
        "meta": meta, "hot_idx": hot_idx, "hot_label": hot_label,
        "hot_long": hot_long, "hot_short": hot_short, "hot_peak": hot_peak,
        "ctrl": ctrl, "ctrl_devices": ctrl_devices,
        "core": core, "mod_proj": mod_proj, "mod_pack": mod_pack,
        "bal": bal, "bal_order": list(bal.keys()),
        "imbalance": (round(max(w_long) / min(w_long), 2)
                      if len(w_long) > 1 and min(w_long) > 0 else None),
        "grand": sum(done),
    }


# ============================================================================
# 六、模板注册表
# ============================================================================

TEMPLATES = [
    {
        "id": "equip_load",
        "name": "设备模块负载与堵塞点",
        "desc": "看全天活儿什么时候涌进来、哪个模块被压满、堵点在哪",
        "icon": "📊",
        "output": "分析报告_设备模块负载与堵塞点_{date}.html",
        "opts": [],
    },
    {
        "id": "speed_chain",
        "name": "免疫线实际测试速度·证据链",
        "desc": "队列重建锁定饱和窗，算出模块真实速度，含均衡化模拟",
        "icon": "🧭",
        "output": "分析报告_免疫线实际测试速度证据链_{date}.html",
        "opts": [
            {"key": "nominal", "label": "单模块标称速度（测试/h）", "type": "number",
             "default": 270, "hint": "i6000 系列常用 270；填小了会低估饱和度"},
            {"key": "mod_count", "label": "真机模块数（留空=自动判）", "type": "number",
             "default": "",
             "hint": "把多台设备的数据合并时，同名模块会撞名（如两台 3联机都有 M1），"
                     "此时必须填真机模块总数，否则产能算少、速度虚高。"
                     "自动判定会按「来源文件×模块名」的组合数来估。"},
            {"key": "window", "label": "测速窗口", "type": "text", "default": "auto",
             "hint": "auto = 自动识别饱和窗；或手填 14,20 表示 14:00–20:00"},
            {"key": "control", "label": "对照组模块（逗号分隔，可空）", "type": "text",
             "default": "", "hint": "填了之后这些模块灰显、不作速度解读（如 2联机）"},
        ],
    },
]

TEMPLATE_BY_ID = {t["id"]: t for t in TEMPLATES}


def build(columns, rows, template_id, opts=None):
    """按模板 id 计算报告数据。返回一个大 dict（直接喂给渲染器）。"""
    tpl = TEMPLATE_BY_ID.get(template_id)
    if not tpl:
        raise ValueError("没有这个报告模板：%s" % template_id)
    opts = opts or {}
    fields = detect_fields(columns)
    fr = field_report(columns, template_id)
    if not fr["ok"]:
        raise ValueError(
            "这份数据缺少报告必需的列：\n\n  · %s\n\n"
            "报告模板「%s」需要：%s。\n"
            "建议在「汇总提取」里确认这些列已勾选，或换一份数据。"
            % ("\n  · ".join(fr["missing"]), tpl["name"],
               "、".join(fr["missing"])))

    if template_id == "equip_load":
        data = build_equip_load(columns, rows, fields, opts)
    elif template_id == "speed_chain":
        data = build_speed_chain(columns, rows, fields, opts)
    else:
        raise ValueError("模板 %s 还没有实现计算逻辑" % template_id)

    data["tpl"] = tpl
    data["fields"] = fields
    data["field_report"] = fr
    data["generated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    return data
