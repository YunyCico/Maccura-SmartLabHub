# -*- coding: utf-8 -*-
"""
====================================================================
 分析报告 · HTML 渲染器（v2.1.0）
====================================================================
把 report_templates.build() 算出来的 dict 渲染成一份**单文件自包含 HTML**
报告 —— 形态严格对齐「成都美年设备运行分析」那一套：

  标题 + 副标题（数据来源/口径）
  ↓
  结论 banner（橙色，结论先行）
  ↓
  流程链胶囊（① → ② → ③ …）
  ↓
  结果汇总表（可独立摘用）
  ↓
  KPI 卡片（顶部 3px 彩条，r/o/g/t 四色）
  ↓
  逐节证据 card（编号 + 标题 + 读图 note + ECharts 图 + 表格）
  ↓
  可落地建议（三张 .cal 色块）
  ↓
  口径说明 footer

设计约定：
  1. 后端拼字符串。报告是独立文件，没有前端 JS 状态可利用，
     必须在这里把 HTML 全拼出来。
  2. 只引用 CDN（ECharts 5.5.0，双源回退）。断网时图空着，
     但表、KPI、结论、口径说明全在，报告依然能读。
  3. 所有数字都先过 fmt()，不留裸 float。
  4. 术语用 .kw + data-tip 做内联 hover 解释，不另起解释段。

本文件被 app.py import，不单独运行。
====================================================================
"""
import html
import json

from .report_templates import fmt, pct, _unit_mod as _mod_of

ECHARTS_CDN_1 = "https://cdn.bootcdn.net/ajax/libs/echarts/5.5.0/echarts.min.js"
ECHARTS_CDN_2 = "https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"

# 与美年报告同源的配色
CSS = """
  :root{
    --bg:#f5f7fa; --card:#ffffff; --ink:#1f2d3d; --ink2:#5e6d82; --line:#e8ecf1;
    --red:#e02020; --orange:#f59a23; --green:#07c160; --blue:#2f7ce0; --teal:#13a8a8; --purple:#7b5cd6;
  }
  *{box-sizing:border-box;margin:0;padding:0;font-family:"Microsoft YaHei","PingFang SC",sans-serif;}
  body{background:var(--bg);color:var(--ink);padding:24px;}
  .wrap{max-width:1280px;margin:0 auto;}
  header{margin-bottom:18px;}
  header h1{font-size:22px;}
  header .sub{color:var(--ink2);font-size:13px;margin-top:6px;line-height:1.7;}
  .verdict{background:#fff6ea;border-left:5px solid var(--orange);border-radius:10px;
    padding:14px 18px;font-size:14px;line-height:1.9;margin:14px 0;
    box-shadow:0 1px 3px rgba(31,45,61,.06);}
  .verdict b{color:#b26a00;}
  .verdict .ttl{display:block;font-weight:700;color:#b26a00;margin-bottom:4px;font-size:14px;}
  .chain{display:flex;gap:8px;margin:14px 0;flex-wrap:wrap;align-items:center;}
  .chain span{background:#fff;border:1px solid var(--line);border-radius:16px;
    padding:5px 14px;font-size:12.5px;color:var(--ink2);}
  .chain span.arrow{border:none;background:none;padding:0;color:var(--ink2);}
  .kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin:16px 0;}
  .kpis.n4{grid-template-columns:repeat(4,1fr);}
  .kpis.n6{grid-template-columns:repeat(6,1fr);}
  .kpi{background:var(--card);border-radius:10px;padding:14px 16px;
    box-shadow:0 1px 3px rgba(31,45,61,.06);border-top:3px solid var(--blue);}
  .kpi.r{border-top-color:var(--red);} .kpi.o{border-top-color:var(--orange);}
  .kpi.g{border-top-color:var(--green);} .kpi.t{border-top-color:var(--teal);}
  .kpi.p{border-top-color:var(--purple);}
  .kpi .v{font-size:21px;font-weight:700;margin:4px 0 2px;}
  .kpi .l{font-size:12px;color:var(--ink2);}
  .kpi .s{font-size:11px;color:var(--ink2);margin-top:2px;line-height:1.5;}
  .card{background:var(--card);border-radius:10px;padding:16px 18px;
    box-shadow:0 1px 3px rgba(31,45,61,.06);margin-bottom:16px;}
  .card h2{font-size:15px;margin-bottom:4px;}
  .card h2 .no{color:var(--blue);margin-right:6px;}
  .card .note{font-size:12px;color:var(--ink2);margin-bottom:8px;line-height:1.75;}
  .card .note b{color:var(--red);}
  .chart{width:100%;height:360px;}
  .chart.tall{height:420px;}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px;}
  table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:8px;}
  th,td{border:1px solid var(--line);padding:6px 8px;text-align:center;}
  th{background:#f0f4f8;}
  td.hot{background:#fde8e8;color:var(--red);font-weight:700;}
  td.warm{background:#fef4e6;color:#b26a00;font-weight:600;}
  td.ok{color:var(--ink2);}
  td.mute{color:#b4b2a9;}
  td b{color:var(--red);}
  .calib{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;}
  .cal{border-radius:8px;padding:12px 14px;font-size:12.5px;line-height:1.85;
    border-left:4px solid var(--blue);background:#f5f9ff;}
  .cal.t{border-left-color:var(--teal);background:#f0fbf8;}
  .cal.o{border-left-color:var(--orange);background:#fff8f0;}
  .cal.g{border-left-color:var(--green);background:#f6fff9;}
  .cal > b:first-child{display:block;font-size:13px;margin-bottom:4px;}
  .cal b{color:var(--red);}
  .cal.t b{color:#0a7a6e;} .cal.o b{color:#b26a00;}
  .cal.g b{color:#0a7a3d;} .cal.g > b:first-child{color:#0a7a3d;}
  .aside{background:#fffbe8;border:1px dashed #e3cf7a;border-radius:8px;
    padding:10px 12px;font-size:12px;color:#7a6320;line-height:1.85;margin-top:8px;}
  .aside b{color:#9a7d16;}
  .analogy{background:#f0fbf8;border-left:4px solid var(--teal);border-radius:8px;
    padding:12px 16px;font-size:13px;line-height:1.9;margin:10px 0;}
  .analogy b{color:#0a7a6e;}
  .fix{background:#fdeeee;border:1px dashed #e0a0a0;border-radius:8px;
    padding:10px 14px;font-size:12.5px;color:#7a2b2b;margin:10px 0;line-height:1.85;}
  .fix b{color:#a02020;}
  .findings{display:grid;grid-template-columns:1fr 1fr;gap:12px;}
  .sumtbl{margin:16px 0;}
  .sumtbl td{font-size:12.5px;text-align:left;line-height:1.85;padding:9px 12px;}
  .sumtbl th{font-size:12.5px;}
  .sumtbl .ic-td{font-size:20px;width:56px;text-align:center;}
  .sumtbl td:nth-child(2){white-space:nowrap;}
  footer{color:var(--ink2);font-size:12px;margin-top:8px;line-height:1.85;
    background:var(--card);border-radius:10px;padding:14px 18px;
    box-shadow:0 1px 3px rgba(31,45,61,.06);}
  footer b{color:var(--ink);}
  .kw{border-bottom:1px dashed var(--blue);cursor:help;position:relative;color:inherit;}
  .kw:hover::after{content:attr(data-tip);position:absolute;left:50%;
    transform:translateX(-50%);bottom:130%;background:#243447;color:#fff;
    padding:9px 13px;border-radius:8px;font-size:12px;line-height:1.7;
    text-align:left;width:max-content;max-width:300px;white-space:normal;
    z-index:99;box-shadow:0 6px 16px rgba(31,45,61,.28);font-weight:400;}
  .kw:hover::before{content:'';position:absolute;left:50%;transform:translateX(-50%);
    bottom:118%;border:6px solid transparent;border-top-color:#243447;z-index:99;}
  @media(max-width:900px){
    .kpis,.kpis.n4,.kpis.n6{grid-template-columns:repeat(2,1fr);}
    .grid2,.findings{grid-template-columns:1fr;}
    .calib{grid-template-columns:1fr;}
    .sumtbl td:nth-child(2){white-space:normal;}
  }
"""


def esc(v):
    """HTML 转义。None 变空串。"""
    if v is None:
        return ""
    return html.escape(str(v), quote=True)


def kw(text, tip):
    """内联术语：hover 出提示，不另起解释段。"""
    return ('<span class="kw" data-tip="%s">%s</span>'
            % (esc(tip), esc(text)))


def kpi(label, value, sub="", color=""):
    return ('<div class="kpi%s"><div class="l">%s</div><div class="v">%s</div>'
            '<div class="s">%s</div></div>'
            % (color, esc(label), esc(value), esc(sub)))


def table(headers, rows, cls=""):
    """headers: [str]；rows: [[cell_html]]（cell 里可带标签，故不转义）。"""
    h = "".join("<th>%s</th>" % esc(x) for x in headers)
    body = []
    for r in rows:
        body.append("<tr>" + "".join("<td>%s</td>" % c for c in r) + "</tr>")
    return ('<table class="%s"><tr>%s</tr>%s</table>'
            % (esc(cls), h, "".join(body)))


# ---------------------------------------------------------------------------
# 模板① 设备模块负载与堵塞点
# ---------------------------------------------------------------------------

def render_equip_load(d):
    """模板①：设备模块负载与堵塞点。"""
    slots = d["slots"]
    mods = d["mod_list"]
    st = d["slot_total"]
    grand = d["grand"]
    hot = d["heat"]

    # ---------- v2.4.0 多天口径 ----------
    multi_day = bool(d.get("multi_day"))
    n_days = int(d.get("n_days") or 1)
    days = d.get("days") or []
    # 措辞：单天叫"全天"，多天叫"N 天合计"
    W_DAY = "全天" if not multi_day else "%d 天合计" % n_days
    W_DAY_SHORT = "全天" if not multi_day else "多天合计"

    # ---------- 结论 banner ----------
    pk = d["peak_slot"]
    pkv = d["peak_val"]
    pk_share = (pkv * 100.0 / grand) if grand else 0
    n_slots = len(slots)
    top_n = d["top_n"]
    top_share = d["top_share"]
    imbal = d["imbalance"]
    od = d["overload"]
    lull = d["gap_hours"]

    if imbal and imbal >= 1.15:
        imbal_txt = ("模块之间不均衡：最忙模块是全闲模块的 <b>%.2f 倍</b>"
                     "（%s）" % (imbal, " vs ".join(
                         "%s %s" % (m, fmt(t))
                         for m, t in zip(mods, d["mod_total"]))))
    else:
        imbal_txt = ("模块之间基本均衡：极差 <b>%.2f 倍</b>，没有明显的单点瓶颈"
                     % (imbal or 0))

    if multi_day:
        # 多天：结论先说"跨天总体"，再点出最忙/最闲的一天
        d_tot = [x["grand"] for x in days]
        busiest_d = max(days, key=lambda x: x["grand"]) if days else None
        lightest_d = min(days, key=lambda x: x["grand"]) if days else None
        day_span_txt = ""
        if busiest_d and lightest_d and lightest_d["grand"] > 0:
            day_span_txt = ("最忙的一天是 <b>%s</b>（%s 条）、最闲的是 <b>%s</b>（%s 条），"
                            "相差 <b>%.2f 倍</b>。"
                            % (esc(busiest_d["label"]), fmt(busiest_d["grand"]),
                               esc(lightest_d["label"]), fmt(lightest_d["grand"]),
                               busiest_d["grand"] * 1.0 / lightest_d["grand"]))
        verdict = (
            "<span class='ttl'>结论先行</span>"
            "这份数据横跨 <b>%d 天</b>（%s ～ %s），合计完成 <b>%s</b> 个测试，"
            "平均每天 <b>%s</b> 个。%s"
            "跨天看，峰值集中在 <b>%s</b>（合计 %s 行，占 %s %.1f%%）。"
            "%s；%s。"
            % (n_days, esc(days[0]["label"] if days else "—"),
               esc(days[-1]["label"] if days else "—"),
               fmt(grand), fmt(int(round(grand / n_days)) if n_days else 0),
               day_span_txt,
               esc(pk), fmt(pkv), W_DAY, pk_share,
               ("时段分布存在极端集中" if top_share >= 60 else "时段分布较为平缓"),
               ("检测到 <b>%d 处单点过载</b>，堵点见下表" % len(od) if od
                else "未检测到单点过载")))
    else:
        verdict = (
            "<span class='ttl'>结论先行</span>"
            "全天 %s 个时段共完成 <b>%s</b> 个测试。"
            "活儿高度集中在 %s 之后的窗口：峰值 <b>%s 时段 %s 行</b>（占全天 %.1f%%），"
            "最忙的 %d 个时段吃掉全天 <b>%.1f%%</b> 的量。"
            "时段分布上%s；%s。"
            % (n_slots, fmt(grand), slots[0] if slots else "—",
               esc(pk), fmt(pkv), pk_share, top_n, top_share,
               ("存在极端集中" if top_share >= 60 else "较为平缓"),
               ("检测到 <b>%d 处单点过载</b>，堵点见下表" % len(od) if od
                else "未检测到单点过载")))

    # ---------- 流程链 ----------
    chain_items = ["① 时段到单节奏", "② 模块时段负载",
                   "③ 负载热力图", "④ 单点过载定位",
                   "⑤ TOP10 项目", "⑥ 高峰与低谷"]
    if multi_day:
        chain_items.insert(1, "② 逐日对比")
    chain = ""
    for i, t in enumerate(chain_items):
        if i:
            chain += '<span class="arrow">→</span>'
        chain += "<span>%s</span>" % esc(t)

    # ---------- 结果汇总表 ----------
    sum_rows = [
        '<td class="ic-td">📦</td>',
        "<td><b>全天完成量</b></td>",
        "<td>%s 个测试，跨 %s 个时段、%s 个模块；单时段均值 %s</td>"
        % (fmt(grand), n_slots, len(mods), fmt(grand / n_slots if n_slots else 0)),
    ]
    sum_tbl = table(["", "结论", "关键依据（数字）"], [
        sum_rows,
        ['<td class="ic-td">⏰</td>',
         "<td><b>高峰在 %s</b></td>" % esc(pk),
         "<td>峰值 %s 行（占全天 %.1f%%）；最忙 %d 段合计占 %.1f%%</td>"
         % (fmt(pkv), pk_share, top_n, top_share)],
        ['<td class="ic-td">⚖️</td>',
         "<td><b>模块均衡度</b></td>",
         "<td>极差 %s；%s</td>"
         % (("%.2f 倍" % imbal) if imbal else "—",
            "存在单点瓶颈" if (imbal or 0) >= 1.15 else "无明显瓶颈")],
        ['<td class="ic-td">🎯</td>',
         "<td><b>%s</b></td>" % ("单点过载 %d 处" % len(od) if od else "无单点过载"),
         "<td>%s</td>" % (", ".join("%s·%s 占 %s%%" % (o["module"], o["slot"], o["share"])
                                    for o in od[:3]) if od else "各时段负载分散")],
        ['<td class="ic-td">💤</td>',
         "<td><b>低谷时段</b></td>",
         "<td>%s</td>" % (("%s 仅 %s 行" % (lull["label"], fmt(lull.get("total", 0))))
                          if lull.get("slots") else "全天无显著低谷")],
    ], cls="sumtbl")

    # ---------- KPI ----------
    busiest = max(range(len(mods)), key=lambda i: d["mod_total"][i]) if mods else 0
    if multi_day:
        d_tot = [x["grand"] for x in days]
        kpis = (kpi("合计完成量", fmt(grand),
                    "%d 天 · 共 %d 个时段 · %d 个模块" % (n_days, n_slots, len(mods)))
                + kpi("平均每天", fmt(int(round(grand / n_days)) if n_days else 0),
                      "单日区间 %s ～ %s" % (fmt(min(d_tot)), fmt(max(d_tot))) if d_tot else "", " o")
                + kpi("峰值时段", pk, "%s %.1f%%" % (W_DAY_SHORT, pk_share), " r")
                + kpi("最忙模块", mods[busiest] if mods else "—",
                      "%s 行 · 占 %.1f%%" % (fmt(d["mod_total"][busiest] if mods else 0),
                                            d["mod_share_pct"][busiest] if mods else 0), " p")
                + kpi("模块极差", ("%.2f 倍" % imbal) if imbal else "—",
                      "最忙 / 最闲", " t"))
    else:
        kpis = (kpi("全天完成量", fmt(grand), "共 %d 个时段 · %d 个模块" % (n_slots, len(mods)))
                + kpi("峰值时段", pk, "%s 行 · 占全天 %.1f%%" % (fmt(pkv), pk_share), " r")
                + kpi("集中度", "%.1f%%" % top_share,
                      "最忙 %d 段 / 全天" % top_n, " o")
                + kpi("最忙模块", mods[busiest] if mods else "—",
                      "%s 行 · 占 %.1f%%" % (fmt(d["mod_total"][busiest] if mods else 0),
                                            d["mod_share_pct"][busiest] if mods else 0), " p")
                + kpi("模块极差", ("%.2f 倍" % imbal) if imbal else "—",
                      "最忙 / 最闲", " t"))

    # ---------- ① 到单节奏 ----------
    s1 = ("口径：按「%s」把每条记录归到所在小时桶，%s。柱＝该时段完成测试数，"
          "折线＝占%s比例。%s"
          % (esc(d["columns_used"].get("time") or "时间"),
             ("并已按天隔离 —— 不同天的同一小时不会互相叠加" if multi_day
              else "落进同一个小时桶"),
             W_DAY,
             ("<b>%s 是%s的峰</b>，占 %.1f%%。" % (esc(pk), W_DAY, pk_share))
             if pk else ""))

    # ---------- 逐日对比（仅多天）----------
    day_tbl = None
    if multi_day:
        dr = []
        for x in days:
            dr.append([
                "<td><b>%s</b></td>" % esc(x["label"]),
                "<td>%s</td>" % fmt(x["grand"]),
                "<td>%s</td>" % ("%.1f%%" % (x["grand"] * 100.0 / grand if grand else 0)),
                "<td>%s</td>" % esc(x["peak_slot"] or "—"),
                "<td>%s</td>" % fmt(x["peak_val"]),
                "<td>%s</td>" % (fmt(len(x["slots"])) if x.get("slots") else "—"),
                "<td>%s</td>" % (esc(x["span_label"]) or "—"),
                "<td>%s</td>" % ("%.2f 倍" % x["imbalance"] if x.get("imbalance") else "—"),
                "<td>%s</td>" % (esc(x["gap_hours"].get("label") or "无")
                                 if x.get("gap_hours") else "无"),
            ])
        day_tbl = table(
            ["日期", "完成量", "占比", "峰值时段", "峰值量", "时段数", "跨度",
             "模块极差", "低谷"], dr)
        s_day = ("下表把每一天单独算了一遍（而不是把多天合并成一个「全天」）。"
                 "<b>时段的量已按天隔离</b>：比如 09/19 09:00–10:00 和 "
                 "09/20 09:00–10:00 是两个独立的格子，不会相加。"
                 "「模块极差」＝当天最忙模块 ÷ 最闲模块，用来判断是不是某一天 "
                 "分配特别偏。")

    # ---------- ② 模块时段负载 ----------
    s2 = ("口径：%s＝该模块在该时段的完成量 ÷ 全天最忙模块完成量（相对口径，"
          "因为本工具没有单模块标称产能参数）。"
          "若要算绝对产能占比，请用模板「免疫线实际测试速度·证据链」。"
          % kw("负载", "此处是相对负载：把每个模块在全天的完成量对最忙模块归一。"
                       "它反映的是「谁比谁忙」，不是「占标称产能的百分之几」。"))

    # ---------- ③ 热力图 ----------
    s3 = ("口径：每格＝该模块该时段完成量 ÷ 该时段内最大模块完成量。"
          "🔴 ≥0.9 接近该时段上限 ｜ 🟠 0.6–0.9 ｜ 其余正常。")

    # ---------- ④ 过载 ----------
    if od:
        ov_rows = []
        for o in od:
            cl = "hot" if o["share"] >= 60 else "warm"
            ov_rows.append([
                "<td>%s</td>" % esc(o["module"]),
                "<td>%s</td>" % esc(o["slot"]),
                '<td class="%s">%s</td>' % (cl, fmt(o["count"])),
                '<td class="%s">%.1f%%</td>' % (cl, o["share"]),
                "<td>%s</td>" % fmt(o["slot_total"]),
            ])
        ov_tbl = table(["模块", "时段", "该模块完成", "占该时段", "该时段总量"], ov_rows)
    else:
        ov_tbl = ('<div class="aside">✅ 没有时段的单一模块占比达到 40%%，'
                  '说明负载分布较散，不存在明显的单点过载。</div>')
    s4 = ("判定规则：某模块在某时段的完成量占该时段总量的 <b>≥40%%</b>，"
          "且该时段总量 ≥3 条，才记为%s。占比条数按从高到低排序。"
          % kw("过载点", "某个模块在某个时段吃掉了该时段绝大部分的量，"
                        "其余模块被挤到一边 —— 这就是堵点。"))

    # ---------- ⑤ TOP10 ----------
    tp = d["top_proj"]
    tp_rows = []
    for i, (name, v, p) in enumerate(tp):
        cl = "hot" if i == 0 else ("warm" if p >= 8 else "ok")
        tp_rows.append([
            "<td>%d</td>" % (i + 1),
            "<td style='text-align:left'>%s</td>" % esc(name),
            '<td class="%s">%s</td>' % (cl, fmt(v)),
            "<td>%.1f%%</td>" % p,
        ])
    tp_tbl = (table(["#", "项目名称", "测试数", "占全天"], tp_rows)
              if tp_rows else '<div class="aside">这份数据里没有项目名称列。</div>')
    top1 = tp[0][0] if tp else "—"
    top1p = tp[0][2] if tp else 0
    top5p = sum(x[2] for x in tp[:5])
    s5 = ("口径：按「%s」列直接计数，不做去重。"
          "%s 是最大的单项（%s，%.1f%%）；前 5 项合计占全天 <b>%.1f%%</b>。"
          % (esc(d["columns_used"].get("project") or "项目名称"),
             esc(top1), fmt(tp[0][1]) if tp else "—", top1p, top5p))

    # ---------- ⑥ 高峰与低谷 ----------
    if lull.get("slots"):
        low = ("全天有明显低谷：<b>%s</b> 只有 %s 行，"
               "说明这段时间系统基本空转。"
               % (esc(lull["label"]), fmt(lull.get("total", 0))))
    else:
        low = "全天各时段量级相差不大，没有明显的空转窗口。"
    hi = ("高峰集中在 <b>%s</b>（%s 行）。" % (esc(pk), fmt(pkv)) if pk else "")
    s6 = hi + low

    # ---------- 建议 ----------
    adv = []
    if od:
        worst = od[0]
        adv.append(
            '<div class="cal"><b>1｜疏导 %s 在 %s 的压力（治本）</b>'
            "该模块在该时段吃掉了 %.1f%% 的量（%s 行 / 时段共 %s 行）。"
            "把该项目包里量最大的项目分流到同时段较闲的模块，"
            "可消除这个%s。</div>"
            % (esc(worst["module"]), esc(worst["slot"]), worst["share"],
               fmt(worst["count"]), fmt(worst["slot_total"]),
               kw("单点瓶颈", "被压得最狠的那一个模块·时段组合，"
                             "它决定了整条线能跑多快。")))
    else:
        adv.append(
            '<div class="cal"><b>1｜当前无单点瓶颈</b>'
            "各模块在各时段的负载未出现明显偏斜，暂不需要做分配再平衡。"
            "继续用同一口径每日跟踪即可。</div>")

    if imbal and imbal >= 1.15:
        idle_i = min(range(len(mods)), key=lambda i: d["mod_total"][i])
        adv.append(
            '<div class="cal t"><b>2｜把量从 %s 挪向 %s（%s）</b>'
            "%s %s %s 行、%s %s %s 行，相差 %.2f 倍。"
            "把高量模块的一部分项目包移到同设备的低量模块，"
            "整机吞吐不变、可靠性提升。</div>"
            % (esc(mods[busiest]), esc(mods[idle_i]),
               kw("削峰", "把高峰时段压在单个模块上的工作量，"
                          "分摊到同时段还有余量的其他模块上。"),
               esc(mods[busiest]), W_DAY, fmt(d["mod_total"][busiest]),
               esc(mods[idle_i]), W_DAY, fmt(d["mod_total"][idle_i]), imbal))
    else:
        adv.append(
            '<div class="cal t"><b>2｜模块已较均衡</b>'
            "最忙与最闲模块相差 %.2f 倍，处在合理范围内，"
            "优先保证高峰时段的收货节奏即可。</div>" % (imbal or 0))

    adv.append(
        '<div class="cal o" style="grid-column:1/-1"><b>3｜数据升级（让负载可日常监控）</b>'
        "当前是按「%s」归到小时桶的完成口径，还没有单模块标称产能，"
        "所以只能算<b>相对负载</b>。若要得到「模块实际速度 / 产能占比」，"
        "请另跑模板「免疫线实际测试速度·证据链」并填好标称速度参数。</div>"
        % esc(d["columns_used"].get("time") or "时间"))

    # ---------- 口径 footer ----------
    if multi_day:
        footer_head = (
            "<b>口径与方法：</b>① 时间列＝「%s」，<b>识别到 %d 天</b>"
            "（%s ～ %s），共 %d 个时段（%s），解析失败 %d 条已剔除；"
            % (esc(d["columns_used"].get("time") or "—"), n_days,
               esc(days[0]["label"] if days else "—"),
               esc(days[-1]["label"] if days else "—"),
               len(slots),
               esc("、".join(slots[:6]) + ("…" if len(slots) > 6 else "")),
               d["bad_time"]))
        footer_mid = (
            "② <b>时段已按天隔离</b>：不同天的同一小时是两个独立的格子，"
            "不会相加；报告正文的「逐日对比」给出每天单独的结果。"
            "③ 模块列＝「%s」，共 %s 个模块；④ 项目列＝「%s」。"
            "⑤ 负载为<b>相对口径</b>（对%s最忙模块归一），本模板不含标称产能参数，"
            "故不作绝对产能解读。"
            % (esc(d["columns_used"].get("module") or "—"), len(mods),
               esc(d["columns_used"].get("project") or "—"), W_DAY))
        footer_tail = (
            "⑥ 报告为单文件自包含，图表依赖 ECharts CDN，无网络时表格与结论仍可读。"
            "<br><b>生成时间：</b>%s　·　分析行数：%s"
            % (esc(d["generated_at"]), fmt(d["row_count"])))
        footer = footer_head + footer_mid + footer_tail
    else:
        footer = (
            "<b>口径与方法：</b>① 时间列＝「%s」，识别到 %d 个时段（%s），"
            "解析失败 %d 条已剔除；② 模块列＝「%s」，共 %s 个模块；"
            "③ 项目列＝「%s」。④ 负载为<b>相对口径</b>（对全天最忙模块归一），"
            "本模板不含标称产能参数，故不作绝对产能解读。"
            "⑤ 报告为单文件自包含，图表依赖 ECharts CDN，"
            "无网络时表格与结论仍可读。"
            "<br><b>生成时间：</b>%s　·　分析行数：%s"
            % (esc(d["columns_used"].get("time") or "—"), len(slots),
               esc("、".join(slots[:6]) + ("…" if len(slots) > 6 else "")),
               d["bad_time"],
               esc(d["columns_used"].get("module") or "—"), len(mods),
               esc(d["columns_used"].get("project") or "—"),
               esc(d["generated_at"]), fmt(d["row_count"])))

    # ---------- 图表数据 ----------
    charts = [
        {"id": "c1", "tall": True,
         "opt": {
             "tooltip": {"trigger": "axis"},
             "legend": _LEG,
             "grid": {"left": 64, "right": 56, "top": 36, "bottom": 62},
             "xAxis": {"type": "category", "data": slots, **_AX,
                       "axisLabel": {"fontSize": 10, "color": "#5e6d82", "rotate": 40}},
             "yAxis": [{"type": "value", "name": "测试数", **_AX,
                        "splitLine": {"lineStyle": {"color": "#eef2f6"}}},
                       {"type": "value", "name": "占全天%",
                        "axisLabel": {"formatter": "{value}%", "fontSize": 11,
                                      "color": "#5e6d82"},
                        "splitLine": {"show": False}, **_AX}],
             "series": [
                 {"name": "完成测试数", "type": "bar", "barWidth": "52%",
                  "itemStyle": {"color": "#2f7ce0", "borderRadius": [4, 4, 0, 0]},
                  "label": {"show": True, "position": "top", "fontSize": 9,
                            "color": "#2f7ce0"},
                  "data": st},
                 {"name": "占全天%", "type": "line", "yAxisIndex": 1,
                  "symbolSize": 7, "smooth": True,
                  "lineStyle": {"color": "#f59a23", "width": 2},
                  "itemStyle": {"color": "#f59a23"},
                  "data": d["slot_share"]},
             ]}},
        {"id": "c2", "tall": False,
         "opt": {
             "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
             "legend": _LEG,
             "grid": {"left": 122, "right": 46, "top": 34, "bottom": 34},
             "xAxis": {"type": "value", "name": "测试数", **_AX,
                       "splitLine": {"lineStyle": {"color": "#eef2f6"}}},
             "yAxis": {"type": "category", "data": list(reversed(mods)),
                       **_AX, "axisLabel": {"fontSize": 11, "color": "#1f2d3d"}},
             "series": [
                 {"name": "全天完成量", "type": "bar", "barWidth": "54%",
                  "itemStyle": {"color": "#7b5cd6", "borderRadius": [0, 4, 4, 0]},
                  "label": {"show": True, "position": "right", "fontSize": 10.5,
                            "color": "#5e6d82"},
                  "data": list(reversed(d["mod_total"]))},
             ]}},
        {"id": "c4", "tall": True,
         "opt": {
             "tooltip": {"position": "top"},
             "grid": {"left": 96, "right": 30, "top": 42, "bottom": 66},
             "xAxis": {"type": "category", "data": slots, **_AX,
                       "axisLabel": {"fontSize": 10, "color": "#5e6d82", "rotate": 40},
                       "splitArea": {"show": True}},
             "yAxis": {"type": "category", "data": list(reversed(mods)),
                       **_AX, "axisLabel": {"fontSize": 11, "color": "#1f2d3d"},
                       "splitArea": {"show": True}},
             "visualMap": {"min": 0, "max": 1, "calculable": True,
                           "orient": "horizontal", "left": "center", "bottom": 0,
                           "itemHeight": 90,
                           "inRange": {"color": ["#f4f8fd", "#fdf0d8", "#fbd9d9", "#e02020"]},
                           "text": ["高", "低"], "textStyle": {"fontSize": 11}},
             "series": [{"name": "相对负载", "type": "heatmap",
                         "label": {"show": True, "fontSize": 9, "color": "#1f2d3d"},
                         "emphasis": {"itemStyle": {"shadowBlur": 8,
                                                    "shadowColor": "rgba(0,0,0,.3)"}},
                         "data": _heat_data(mods, slots, hot)}]}},
        {"id": "c5", "tall": False,
         "opt": _bar_h(tp[:10], "#13a8a8", "测试数")},
    ]

    # ---------- 逐日对比柱状图（仅多天）----------
    if multi_day:
        charts.insert(1, {
            "id": "c0", "tall": False,
            "opt": {
                "tooltip": {"trigger": "axis"},
                "grid": {"left": 62, "right": 40, "top": 30, "bottom": 58},
                "xAxis": {"type": "category",
                          "data": [x["label"] for x in days], **_AX,
                          "axisLabel": {"fontSize": 10.5, "color": "#5e6d82", "rotate": 30}},
                "yAxis": {"type": "value", **_AX,
                          "axisLabel": {"fontSize": 10.5, "color": "#5e6d82"}},
                "series": [{"name": "当天完成量", "type": "bar", "barWidth": "46%",
                            "itemStyle": {"color": "#4d7cfe", "borderRadius": [4, 4, 0, 0]},
                            "label": {"show": True, "position": "top", "fontSize": 10.5,
                                      "color": "#5e6d82"},
                            "data": [x["grand"] for x in days]},
                           {"name": "当天峰值小时量", "type": "line", "smooth": True,
                            "symbolSize": 7, "lineStyle": {"width": 2, "color": "#e8720c"},
                            "itemStyle": {"color": "#e8720c"},
                            "data": [x["peak_val"] for x in days]}],
            }})

    # 单天时保持原有 6 节；多天时把"逐日对比"插在到单节奏之后
    if multi_day:
        secs = [
            {"h": "① 多天时段到单节奏（合计）", "note": s1, "charts": ["c1"]},
            {"h": "② 逐日对比", "note": s_day, "charts": ["c0"], "html": day_tbl},
            {"h": "③ 各模块%s完成量" % ("合计" if multi_day else "全天"),
             "note": s2, "charts": ["c2"], "half": True},
            {"h": "④ 模块 × 时段 · 相对负载热力图",
             "note": s3, "charts": ["c4"]},
            {"h": "⑤ 单点过载定位", "note": s4, "html": ov_tbl},
            {"h": "⑥ TOP10 检测项目", "note": s5, "charts": ["c5"], "html": tp_tbl},
            {"h": "⑦ 高峰与低谷", "note": s6, "html": _lull_html(d)},
        ]
    else:
        secs = [
            {"h": "① 全天时段到单节奏", "note": s1, "charts": ["c1"]},
            {"h": "② 各模块全天完成量", "note": s2, "charts": ["c2"], "half": True},
            {"h": "③ 模块 × 时段 · 相对负载热力图",
             "note": s3, "charts": ["c4"]},
            {"h": "④ 单点过载定位", "note": s4, "html": ov_tbl},
            {"h": "⑤ TOP10 检测项目", "note": s5, "charts": ["c5"], "html": tp_tbl},
            {"h": "⑥ 高峰与低谷", "note": s6, "html": _lull_html(d)},
        ]

    return _page(
        title="📊 设备模块负载与堵塞点 · 分析报告",
        h1="📊 设备模块负载与堵塞点 · 分析报告",
        sub=("数据源：%s 行　·　时间列「%s」　·　模块「%s」　·　项目「%s」%s<br>"
             "口径：以「%s」把每条记录归入小时桶后按模块/项目汇总%s。"
             "负载为相对口径（对%s最忙模块归一）。"
             % (fmt(d["row_count"]), esc(d["columns_used"].get("time") or "—"),
                esc(d["columns_used"].get("module") or "—"),
                esc(d["columns_used"].get("project") or "—"),
                ("　·　<b>识别到 %d 天</b>" % n_days) if multi_day else "",
                esc(d["columns_used"].get("time") or "—"),
                ("，且<b>按天隔离</b>（不同天的小时桶不合并）" if multi_day else ""),
                W_DAY)),
        verdict=verdict,
        chain=chain,
        sum_tbl=sum_tbl,
        kpis=kpis,
        nkpi=5,
        sections=secs,
        advice='<div class="findings">%s</div>' % "".join(adv),
        footer=footer,
        charts=charts,
        data_js=_json({"slots": slots, "mods": mods, "total": st,
                       "heat": hot, "modTotal": d["mod_total"]}),
    )


def _lull_html(d):
    """高峰/低谷小结块。"""
    rows = [
        ["高峰时段", esc(d["peak_slot"] or "—"), fmt(d["peak_val"]),
         "%.1f%%" % (d["peak_val"] * 100.0 / d["grand"] if d["grand"] else 0)],
        ["最忙 %d 段合计" % d["top_n"], "—",
         "%.1f%%" % d["top_share"], "集中度"],
        ["低谷时段", esc(d["gap_hours"].get("label") or "无"),
         fmt(d["gap_hours"].get("total", 0) or 0), "占全天 %.1f%%" % (
             (d["gap_hours"].get("total", 0) or 0) * 100.0 / d["grand"]
             if d["grand"] else 0)],
        ["活跃时段数", "%d / %d" % (d["active_slots"], len(d["slots"])),
         "跨度 %d 小时" % d["span_hours"], "首末时段之差"],
    ]
    return table(["项", "时段", "数值", "说明"], [
        ["<td style='text-align:left'>%s</td>" % a,
         "<td>%s</td>" % b,
         "<td><b>%s</b></td>" % c,
         "<td class='ok'>%s</td>" % e] for a, b, c, e in rows
    ])


def _heat_data(mods, slots, heat):
    """ECharts heatmap 数据：[x_idx, y_idx(倒序行), value]。"""
    out = []
    n = len(mods) - 1
    for mi in range(len(mods)):
        for si in range(len(slots)):
            out.append([si, n - mi, round(heat[mi][si], 3)])
    return out


# ---------------------------------------------------------------------------
# 模板② 免疫线实际测试速度 · 证据链
# ---------------------------------------------------------------------------

def render_speed_chain(d):
    """模板②：队列重建 → 饱和窗 → 模块速度 → 均衡化。"""
    slots = d["slots"]
    mods = d["mod_list"]
    nominal = d["nominal"]
    done = d["done"]
    util = d["util"]
    queue = d["queue"]
    w_long = d["w_long"]
    w_short = d["w_short"]
    peak = d["peak"]
    meta = d["meta"]
    ctrl = d["ctrl"]
    hot_i = d["hot_idx"]
    hot_label = d["hot_label"]
    grand = d["grand"]

    hot_long = d["hot_long"]
    hot_short = d["hot_short"]
    hot_peak = d["hot_peak"]
    lo_l, hi_l = (min(hot_long), max(hot_long)) if hot_long else (0, 0)
    lo_s, hi_s = (min(hot_short), max(hot_short)) if hot_short else (0, 0)
    pk_max = max(hot_peak) if hot_peak else 0
    hot_pct = [round(v * 100.0 / nominal, 1) for v in hot_long] if nominal else []

    # 非对照组的表观速度范围
    other = [w_long[i] for i, m in enumerate(meta) if not m["ctrl"] and i not in hot_i]
    o_lo, o_hi = (min(other), max(other)) if other else (0, 0)

    # ---------- 结论 banner ----------
    multi_day = bool(d.get("multi_day"))
    n_days = int(d.get("n_days") or 1)
    days_cmp = d.get("days_cmp") or []
    best_day_label = d.get("best_day_label") or ""
    if hot_long:
        day_lead = ""
        if multi_day:
            day_lead = (
                "数据横跨 <b>%d 天</b>，已<b>按天各自重建队列、各自找饱和窗</b>"
                "（跨天累加会把前一天的积压带进第二天，速度会算错）。"
                "饱和窗最大、速度最高的一天是 <b>%s</b>，下面以它作为能力证据；"
                "每天的对照见「逐日对比」。"
                % (n_days, esc(best_day_label)))
        verdict = (
            "<span class='ttl'>结论先行</span>"
            + day_lead +
            "饱和窗 <b>%s</b>（%d 个时段）内，%s 的实际速度是 "
            "<b>%s–%s 测试/h</b>（短窗 %s–%s），标称 %s %s；"
            "短时峰值达 <b>%s</b>，%s。"
            "%s"
            % (esc(d["win_label"]), d["win_hours"], esc(hot_label),
               fmt(lo_l), fmt(hi_l), fmt(lo_s), fmt(hi_s), nominal,
               ("准确甚至偏保守" if pk_max >= nominal else "略有高估，建议下调"),
               fmt(pk_max),
               ("说明模块短时能力 ≥ 标称，%s 是可信的下限估计"
                % ("%.0f–%.0f" % (lo_l, hi_l)) if pk_max >= nominal
                else "峰值未达标称，标称参数可能填大了"),
               ("其余模块 %s 测试/h，属<b>分配节奏</b>而非能力上限。"
                % ("%.0f–%.0f" % (o_lo, o_hi)) if other else "")))
    else:
        verdict = ("<span class='ttl'>结论先行</span>"
                   "本次数据未识别出饱和窗（没有任何时段的完成量接近标称产能），"
                   "说明当日系统未被压满，无法用队列法反推真实速度。"
                   "请核对标称速度参数，或用「测速窗口」手工指定高峰时段。")

    # ---------- 逐日对照表（仅多天）----------
    day_tbl = None
    if multi_day and days_cmp:
        dr = []
        for x in days_cmp:
            spd = x.get("speed_long")
            dr.append([
                "<td><b>%s</b></td>" % esc(x["label"]),
                "<td>%s</td>" % fmt(x["total"]),
                "<td>%s</td>" % fmt(x["peak"] if x.get("peak") is not None else 0),
                "<td>%s</td>" % (esc(x["win_label"]) or "—"),
                "<td>%s</td>" % (("%.0f 测试/h" % spd) if spd else
                                 "<span style='color:#8a94a6'>未达饱和</span>"),
                "<td>%s</td>" % ("<b>%s</b>" % esc(x["label"])
                                 if x["label"] == best_day_label else "—"),
            ])
        day_tbl = table(["日期", "当天完成量", "当日峰值小时", "当天饱和窗",
                         "当天实际速度", "能力证据"], dr)

    # ---------- 流程链 ----------
    chain_txt = ["① 统一口径", "② 队列重建：%s" % (d["win_label"] or "—"),
                 "③ 饱和窗测速：%s" % (("%.0f–%.0f" % (lo_l, hi_l)) if hot_long else "—"),
                 "④ 根因：项目包分配", "⑤ 均衡化模拟"]
    chain = ""
    for i, t in enumerate(chain_txt):
        if i:
            chain += '<span class="arrow">→</span>'
        chain += "<span>%s</span>" % esc(t)

    # ---------- 结果汇总表 ----------
    qpk = max(queue) if queue else 0
    qpk_i = queue.index(qpk) if queue else 0
    sum_tbl = table(["", "结论", "关键依据（数字）"], [
        ['<td class="ic-td">✅</td>',
         "<td><b>标称 %s %s</b></td>" % (nominal,
                                     "准确" if pk_max >= nominal else "偏高"),
         '<td class="ok">%s 实测 <b>%s–%s 测试/h</b>，短时峰值 <b>%s</b>%s</td>'
         % (esc(hot_label), fmt(lo_l), fmt(hi_l), fmt(pk_max),
            "≥ 标称 → 是保守的下限估计" if pk_max >= nominal
            else "＜ 标称 → 标称可能填大了")],
        ['<td class="ic-td">⚖️</td>',
         "<td><b>其余模块是分配节奏</b></td>",
         '<td class="ok">%s 并非能力上限——同型号，仅项目包量少「喂不满」</td>'
         % (("<b>%.0f–%.0f 测试/h</b>" % (o_lo, o_hi)) if other else "—")],
        ['<td class="ic-td">🎯</td>',
         "<td><b>根因＝项目包分配不均</b></td>",
         '<td class="ok">%s 包量最大 → 唯一满转；模块速度极差 <b>%s</b></td>'
         % (esc(hot_label), ("%.2f 倍" % d["imbalance"]) if d["imbalance"] else "—")],
        ['<td class="ic-td">🧩</td>',
         "<td><b>均衡化可解</b></td>",
         "<td class='ok'>把 %s 的项目包分一部分给同设备其余模块，"
         "负载可拉平到 <b>%s</b>，整机吞吐不变、无需加资源</td>"
         % (esc(hot_label), _bal_target_txt(d))],
    ], cls="sumtbl")

    # ---------- KPI ----------
    kpis = (kpi("队列峰值", fmt(qpk),
                "%s 期末积压测试数%s" % (slots[qpk_i] if slots else "—",
                                        "（%s）" % best_day_label if multi_day else ""), " r")
            + kpi("饱和窗", d["win_label"] or "—",
                  "%d 个时段 · 完成 %.0f%% 以上产能%s"
                  % (d["win_hours"], 80, "（%s）" % best_day_label if multi_day else ""), " o")
            + kpi("%s 实际速度" % hot_label,
                  ("%.0f–%.0f" % (lo_l, hi_l)) if hot_long else "—",
                  "短窗 %.0f–%.0f · 标称 %d" % (lo_s, hi_s, nominal) if hot_long else "")
            + kpi("%s 短时峰值" % hot_label, fmt(pk_max),
                  "证明能力 ≥ 标称" if pk_max >= nominal else "未达标称", " t")
            + kpi("均衡化后负载", _bal_target_txt(d),
                  "当前 %s" % _cur_spread_txt(d, nominal), " g"))
    # ---------- 证据① ----------
    e1 = ('<div class="calib">'
          '<div class="cal t"><b>本工具只有一列时间 → 到达/离开同源</b>'
          '这份数据里只有一个时间列「%s」。严格意义上它既是「完成/出结果」时刻，'
          '我们用它同时充当到达与离开。因此本项目<b>不用</b>'
          '「累计到达 − 累计离开」这条公式，'
          '改用「累计完成 − 累计按标称产能应完成」来衡量积压。</div>'
          '<div class="cal"><b>队列 = 累计完成 − 标称产能 × 已过时段</b>'
          '队列 &gt; 0 表示这段时间系统在<b>追赶</b>：按标称产能本该做完的量，'
          '实际还没做完，机器必然满转。此时「每小时完成数」才近似真实速度。</div>'
          '<div class="cal o"><b>「出结果」含审核滞后 → 速度是下限</b>'
          '记录的是出结果时刻，中间还隔着审核确认。'
          '所以用它算出的速度只会<b>低估</b>、不会高估机器能力 —— '
          '结论偏保守、更可信。</div>'
          '</div>'
          '<div class="fix">⚠️ <b>口径更正（必读）：</b>'
          '「成都美年」那份报告用的是两列时间（上机时间 / 出结果时间），'
          '所以队列＝累计到达 − 累计离开。本工具的数据只有一列'
          '「%s」，无法区分到达与离开，故队列改用'
          '<b>「累计完成 − 累计标称产能」</b>这一相对口径。'
          '两条公式得到的「饱和窗」在同一份数据上是一致的'
          '（都锁定"机器干不完"的时段），但积分量级不同，'
          '<b>请勿把两份报告的队列绝对值直接横向比较</b>。</div>'
          '<div class="aside">💡 <b>为什么必须先判队列、再算速度：</b>'
          '上半天活儿少、下半天排队时，全天一平均就把高峰拉低。'
          '只有"队列不为零"的时段，机器才是 100%% 时间在干活，'
          '「每小时完成数」才等于真实速度。</div>'
          % (esc(d["columns_used"].get("time") or "—"),
             esc(d["columns_used"].get("time") or "—")))

    # 跨设备同名模块的提示（只在真的出现时加，不打扰单设备场景）
    if d.get("name_dup"):
        e1 += (
            '<div class="fix">⚠️ <b>已检测到跨设备同名模块：</b>'
            '这份数据里「%s」列显示有 <b>%d</b> 个来源，而模块名只有 '
            '%d 个（%s）。同名模块其实分属<b>不同设备</b>，'
            '例如两台 3联机 各自都有 M1/M2/M3。<br>'
            '本报告已按 <b>%d 个真机模块</b> 计算产能（%s 测试/h），'
            '并把每个「来源 · 模块」当作独立单元单独测速 —— '
            '否则产能会算少一半、速度会虚高约一倍。'
            '若实际机台数不是 %d，请在生成时手工填「真机模块数」参数。</div>'
            % (esc(d["columns_used"].get("source") or "来源"),
               d.get("distinct_units") or 0,
               len(set(d["mod_list"])),
               esc("、".join(sorted({_mod_of(m) for m in d["mod_list"]}))),
               d.get("mod_count") or 0, fmt(d.get("cap") or 0),
               d.get("mod_count") or 0))

    # ---------- 证据② ----------
    e2 = ("口径：柱＝该时段完成量，折线＝该时段相对标称产能（%d × %d 模块 = %s/h）"
          "的利用率，面积＝队列积压。"
          "自动识别的饱和窗取「利用率 ≥ 80%% 的最长连续段」。"
          "<b>本数据识别到 %s（%d 个时段）。</b>"
          % (nominal, len(mods), fmt(d["cap"]),
             esc(d["win_label"]), d["win_hours"]))
    e2 += ('<div class="analogy"><b>一句话原理（餐厅类比）：</b>'
           '想知道后厨最快出菜多快，不能看它平均每小时上了几道菜，'
           '而要看"等位单量一直不为零的那几小时"实际出了多少。'
           '等位大于零，后厨必然在满负荷出菜。'
           '本报告的"等位单量"就是队列曲线。</div>')

    # ---------- 证据③ ----------
    e3 = ("<b>%s实际速度</b> ＝ 窗口内完成测试数 ÷ 窗口时长。"
          "三重口径互相印证：<b>长窗（%s，%d 小时）为可持续速度</b>；"
          "短窗（%s）为高强度速度；峰值小时为短时能力下限。"
          % (kw("实际速度", "饱和窗内完成测试数 ÷ 窗口小时数。因「出结果」含审核滞后，"
                            "真实速度只会 ≥ 算出的数——下限估计，结论保守、不夸大。"),
             esc(d["win_label"]), d["win_hours"], esc(d["win_short_label"])))
    if hot_long and hot_short:
        dev = [abs(a - b) / a * 100 for a, b in zip(hot_long, hot_short) if a]
        dev_max = max(dev) if dev else 0
        e3 += ("窗口敏感性：长窗与短窗差异最大 <b>%.1f%%</b>，%s"
               % (dev_max, "结论稳健" if dev_max <= 12 else "波动较大，建议手工指定窗口复核"))
    spd_rows = []
    for i, m in enumerate(meta):
        gray = m["ctrl"]
        wl, ws, pk = w_long[i], w_short[i], peak[i]
        is_hot = i in hot_i
        cls_l = "mute" if gray else ("hot" if is_hot else "warm")
        cls_h = "ok" if (gray or is_hot or wl >= nominal) else "warm"
        r = [
            "<td>%s</td>" % esc(m["key"]),
            '<td class="%s">%s</td>' % (cls_l, fmt(wl)),
            "<td>%s</td>" % fmt(ws),
            "<td>%s（%s）</td>" % (fmt(pk), esc(_short_slot(d["peak_slot"][i]))),
            '<td class="%s">%.0f%%</td>' % (cls_h, wl * 100.0 / nominal if nominal else 0),
            "<td>%s</td>" % ("<span class='mute'>对照组·不作速度解读</span>"
                             if gray else _judge_html(m, is_hot)),
        ]
        spd_rows.append(r)
    spd_tbl = table(["模块", "饱和窗 %s（%d 小时）" % (d["win_label"], d["win_hours"]),
                     "高强度 %s" % d["win_short_label"], "峰值小时", "vs 标称 %d" % nominal,
                     "判定"], spd_rows)
    if ctrl:
        e3 += ('<div class="aside">💡 <b>备注：</b>%s 已按参数设为'
               '<b>对照组</b>，灰显且不作速度解读。</div>' % esc("、".join(ctrl)))

    # ---------- 证据④ 根因 ----------
    e4 = ("每个模块固定挂钩一批项目（项目包）。"
          "包量大的模块被压满、表观速度接近标称；包量小的模块「喂不满」、"
          "速度被分配节奏限制。<b>这是分配结构问题，不是模块性能差异。</b>")
    pack_rows = []
    for m in mods:
        pk = d["mod_proj"].get(m, {})
        tot = sum(pk.values())
        top = sorted(pk.items(), key=lambda kv: -kv[1])[:5]
        txt = " ＋ ".join("%s %s" % (esc(k), fmt(v)) for k, v in top) or "—"
        pack_rows.append([
            "<td>%s</td>" % esc(m),
            "<td><b>%s</b></td>" % fmt(tot),
            "<td style='text-align:left'>%s</td>" % txt,
            "<td>%s</td>" % (esc(top[0][0]) if top else "—"),
        ])
    pack_tbl = table(["模块", "项目包总量", "包内主要项目（测试数）", "最大单项"], pack_rows)

    # ---------- 证据⑤ 交叉验证 ----------
    ctrl_speed = [w_long[i] for i, m in enumerate(meta) if m["ctrl"]]
    raw_avg = [round(sum(d["grid"][i]) / len(slots), 1) for i in range(len(mods))] if slots else []
    v_rows = []
    v_rows.append([
        "<td>全天裸算平均</td>",
        "<td>%s</td>" % ", ".join("%s %s" % (esc(mods[i]), fmt(raw_avg[i]))
                                  for i in range(len(mods))) if raw_avg else "<td>—</td>",
        "<td>把低谷时段也摊进来 → 系统性低估，<b>证明不能裸算</b></td>",
    ])
    if hot_long:
        v_rows.append([
            "<td>饱和窗速度</td>",
            "<td>%s %s</td>" % (esc(hot_label), "%.0f–%.0f" % (lo_l, hi_l)),
            "<td>只取机器干不完的时段 → 才等于真实速度</td>",
        ])
    v_rows.append([
        "<td>短时峰值</td>",
        "<td>%s</td>" % fmt(pk_max),
        "<td>%s</td>" % ("超过标称 %d → 短时能力 ≥ 标称" % nominal if pk_max >= nominal
                         else "未超标称 → 标称参数偏大"),
    ])
    v_rows.append([
        "<td>极差（最忙/最闲）</td>",
        "<td>%s</td>" % (("%.2f 倍" % d["imbalance"]) if d["imbalance"] else "—"),
        "<td>同型号模块间差异 → 指向分配不均而非性能差异</td>",
    ])
    e5 = table(["证据", "数值", "说明"], v_rows)
    e5 += ('<div class="aside">💡 <b>备注（为什么不能全天平均）：</b>'
           '前几个小时几乎没活、高峰又在排队，一平均就把高峰拉低。'
           '所以「先判队列、再算速度」是必选项，不是可选项。</div>')

    # ---------- 证据⑥ 均衡化 ----------
    e6 = ("把 %s 的项目包按模块数均摊给同设备的其余模块"
          "（<b>只是模拟，不改真实分配</b>），看负载能否拉平。" % esc(hot_label))
    bal_rows = []
    for k, b in d["bal"].items():
        bal_rows.append([
            "<td>%s</td>" % esc(k),
            "<td class='warm'>%s</td>" % " / ".join("%.0f%%" % p for p in b["cur_pct"]),
            "<td class='ok'>≈%.1f%% 均衡</td>" % b["pct"],
            "<td>%s</td>" % ("满转风险消除，整机吞吐不变" if b["pct"] < 100
                             else "仍贴满负荷"),
        ])
    if bal_rows:
        e6 += table(["方案", "当前各模块负载%", "均衡化后", "效果"], bal_rows)
    else:
        e6 += ('<div class="aside">本数据里没有可做均衡化的多模块同设备组，'
               '或所有模块都被指定为对照组。</div>')

    # ---------- 建议 ----------
    adv = []
    if hot_long:
        adv.append(
            '<div class="cal"><b>1｜项目包再平衡（治本）</b>'
            "把 %s 的项目包里量最大的几个项目各分一部分到同设备的其余模块，"
            "%s 从 %s%% 降到约 %s%%，其余模块升到 %s，"
            "消除单点满转瓶颈、整机吞吐不变。</div>"
            % (esc(hot_label), esc(hot_label),
               "%.0f" % (max(hot_pct) if hot_pct else 0),
               "%.0f" % (sum(hot_pct) / len(hot_pct) if hot_pct else 0),
               _bal_target_txt(d)))
    adv.append(
        '<div class="cal t"><b>2｜高峰借道空余模块（削峰）</b>'
        "%s，高峰时段把项目包外的加急标本引导到这些接口，"
        "缓解满转模块的瞬时压力。</div>"
        % (("低谷/非饱和时段的 %s 仍有余量" % esc(hot_label)) if False else
           ("观测到 %s 等模块在饱和窗内仍低于标称" % esc(hot_label)) if other else
           "饱和窗内已无多余模块"))
    adv.append(
        '<div class="cal o" style="grid-column:1/-1"><b>3｜数据升级（让速度可日常监控）</b>'
        "现在只有一列出结果时间、且精确到小时以上，速度只能得下限。"
        "建议从 LIS/仪器导出<b>测试级时间戳（精确到分钟）</b>，"
        "并补一列「标本上机时间」，速度＝测试数 ÷（末条 − 首条时间），"
        "同时把「队列长度、模块实际速度」固化为每日 BI 指标。</div>")

    # ---------- 口径 footer ----------
    _dup_txt = ""
    if d.get("name_dup"):
        _dup_txt = ("　⑦ <b>跨设备同名模块</b>：模块名有 %d 个、但「来源」有 %d 个，"
                    "本报告已按 <b>%d 个真机模块</b>计产能，"
                    "并按「来源 · 模块」分别测速（见证据①）；"
                    % (len(set(d["mod_list"])), d.get("distinct_units") or 0,
                       d.get("mod_count") or 0))
    footer = (
        "<b>口径与方法：</b>① 时间列＝「%s」（本工具唯一时间列，"
        "既当到达又当离开；解析失败 %d 条已剔除）；"
        "② 模块列＝「%s」，共 %d 个分析单元，"
        "标称 %d 测试/h/模块 → 全线 %s/h；"
        "③ 队列(t) ＝ 累计完成 − 标称产能 × 已过时段数 —— "
        "<b>非</b>「累计到达 − 累计离开」，因本数据无独立到达列，"
        "故队列绝对量级不可与两列时间的报告横向比较；"
        "④ 饱和窗：利用率 ≥80%% 的最长连续段（本次 %s，%d 个时段）；"
        "⑤ 「出结果」含审核滞后，所得速度为真实测试速度的<b>下限估计</b>；"
        "⑥ 报表为单文件自包含，图表依赖 ECharts CDN，无网络时表格与结论仍可读。%s"
        "<br><b>生成时间：</b>%s　·　分析行数：%s"
        % (esc(d["columns_used"].get("time") or "—"), d["bad_time"],
           esc(d["columns_used"].get("module") or "—"), len(mods),
           nominal, fmt(d["cap"]), esc(d["win_label"]), d["win_hours"],
           _dup_txt, esc(d["generated_at"]), fmt(d["row_count"])))
    if multi_day:
        footer = (
            "<b>口径与方法（多天）：</b>① 时间列＝「%s」，<b>识别到 %d 天</b>"
            "（%s ～ %s），解析失败 %d 条已剔除；"
            "② <b>队列与饱和窗按天隔离</b>：队列是「当天从零起算」的积压，"
            "跨天累加会把前一天的积压带进第二天、速度算错，"
            "故每天各自重建队列、各自找饱和窗，再取速度最高的 <b>%s</b> 作为能力证据"
            "（每天的对照见「逐日对比」）；"
            "③ 模块列＝「%s」，共 %d 个分析单元，标称 %d 测试/h/模块 → 全线 %s/h；"
            "④ 「出结果」含审核滞后，所得速度为真实测试速度的<b>下限估计</b>；"
            "⑤ 报表为单文件自包含，图表依赖 ECharts CDN，无网络时表格与结论仍可读。%s"
            "<br><b>生成时间：</b>%s　·　分析行数：%s"
            % (esc(d["columns_used"].get("time") or "—"), n_days,
               esc(days_cmp[0]["label"] if days_cmp else "—"),
               esc(days_cmp[-1]["label"] if days_cmp else "—"),
               d["bad_time"], esc(best_day_label),
               esc(d["columns_used"].get("module") or "—"), len(mods),
               nominal, fmt(d["cap"]),
               _dup_txt, esc(d["generated_at"]), fmt(d["row_count"])))

    # ---------- 图表 ----------
    n_slot = len(slots)
    charts = [
        {"id": "ec1", "tall": True,
         "opt": {
             "tooltip": {"trigger": "axis"},
             "legend": _LEG,
             "grid": {"left": 64, "right": 64, "top": 36, "bottom": 62},
             "xAxis": {"type": "category", "data": slots, **_AX,
                       "axisLabel": {"fontSize": 10, "color": "#5e6d82", "rotate": 40}},
             "yAxis": [{"type": "value", "name": "测试数", **_AX,
                        "splitLine": {"lineStyle": {"color": "#eef2f6"}}},
                       {"type": "value", "name": "队列积压",
                        "splitLine": {"show": False}, **_AX}],
             "series": [
                 {"name": "每小时完成量", "type": "bar", "barWidth": "48%",
                  "itemStyle": {"color": "#85B7EB"},
                  "data": done},
                 {"name": "利用率%（右轴）", "type": "line", "yAxisIndex": 1,
                  "symbolSize": 7, "smooth": True,
                  "lineStyle": {"color": "#5DCAA5", "width": 2},
                  "itemStyle": {"color": "#5DCAA5"},
                  "data": util},
                 {"name": "队列积压", "type": "line", "yAxisIndex": 1,
                  "symbolSize": 5, "smooth": True,
                  "lineStyle": {"color": "#e02020", "width": 2},
                  "itemStyle": {"color": "#e02020"},
                  "areaStyle": {"color": "rgba(224,32,32,.16)"},
                  "data": queue},
             ]}},
        {"id": "ec2", "tall": True,
         "opt": {
             "tooltip": {"trigger": "axis",
                         "valueFormatter": "vfmt"},
             "legend": _LEG,
             "grid": {"left": 60, "right": 30, "top": 40, "bottom": 74},
             "xAxis": {"type": "category", "data": mods, **_AX,
                       "axisLabel": {"fontSize": 10.5, "color": "#5e6d82",
                                     "interval": 0, "rotate": 28}},
             "yAxis": {"type": "value", "name": "测试/h",
                       "max": int(max([nominal * 1.35] + peak + [1])),
                       "splitLine": {"lineStyle": {"color": "#eef2f6"}}, **_AX},
             "series": [
                 {"name": "饱和窗 %s" % d["win_label"], "type": "bar",
                  "barWidth": "26%", "itemStyle": {"color": "#2f7ce0"},
                  "label": {"show": True, "position": "top", "fontSize": 9,
                            "color": "#2f7ce0"},
                  "data": [None if i in [j for j, m in enumerate(meta) if m["ctrl"]]
                           else w_long[i] for i in range(len(mods))]},
                 {"name": "高强度 %s" % d["win_short_label"], "type": "bar",
                  "barWidth": "26%", "itemStyle": {"color": "#7FCBEB"},
                  "label": {"show": True, "position": "top", "fontSize": 9,
                            "color": "#5e6d82"},
                  "data": [None if i in [j for j, m in enumerate(meta) if m["ctrl"]]
                           else w_short[i] for i in range(len(mods))]},
                 {"name": "峰值小时（短时能力）", "type": "scatter", "symbolSize": 11,
                  "itemStyle": {"color": "#e02020"},
                  "data": [({"value": [i, peak[i]]}
                            if mod_list_at(meta, i)["ctrl"] is False else
                            {"value": [i, peak[i]], "itemStyle": {"color": "#cfcfcf"}})
                           for i in range(len(mods))]},
                 {"name": "标称 %d/h" % nominal, "type": "line", "symbol": "none",
                  "tooltip": {"show": False},
                  "lineStyle": {"type": "dashed", "color": "#f59a23", "width": 1.5},
                  "data": [nominal] * len(mods)},
             ]}},
        {"id": "ec3", "tall": False, "h": "400px",
         "opt": _stack_opt(mods, d)},
        {"id": "ec4", "tall": False,
         "opt": _bal_opt(d, nominal)},
    ]
    charts = [c for c in charts if c["opt"]]

    return _page(
        title="🧭 %s 实际测试速度 · 证据链报告" % (hot_label or "模块"),
        h1="🧭 %s 实际测试速度 · 证据链" % (hot_label or "免疫线"),
        sub=("数据源：%s 行　·　时间列「%s」　·　模块「%s」（%d 个）　·　"
             "项目「%s」<br>"
             "标称速度：%s 测试/h/模块 → 全线 %s 测试/h　｜　"
             "饱和窗：%s（%d 个时段）"
             % (fmt(d["row_count"]), esc(d["columns_used"].get("time") or "—"),
                esc(d["columns_used"].get("module") or "—"), len(mods),
                esc(d["columns_used"].get("project") or "—"),
                nominal, fmt(d["cap"]), esc(d["win_label"]), d["win_hours"])),
        verdict=verdict, chain=chain, sum_tbl=sum_tbl, kpis=kpis, nkpi=5,
        sections=(
            ([{"h": "证据⓪ 逐日对比：每天单独重建队列",
               "note": ("队列是「当天从零起算」的积压量。若把多天数据连起来累加，"
                        "前一天的积压会被带进第二天，队列曲线失真、饱和窗跨天，"
                        "速度就算错了。所以本报告<b>每天各自重建队列、各自找饱和窗</b>，"
                        "下表是每天的独立结果；正文的证据②～⑥ 以速度最高的 "
                        "<b>%s</b> 作为能力证据。" % esc(best_day_label)),
               "html": day_tbl}] if multi_day else []) +
            [
                {"h": "证据① 统一口径：这份数据的时间列到底代表什么",
                 "note": "先讲清楚数据能支持什么、不能支持什么，再谈结论。",
                 "html": e1},
                {"h": "证据② 队列重建 —— 锁定「饱和窗」",
                 "note": e2, "charts": ["ec1"]},
                {"h": "证据③ 饱和窗完成速率 ＝ 各模块实际测试速度",
                 "note": e3, "charts": ["ec2"], "html": spd_tbl},
                {"h": "证据④ 根因：项目包在各模块上的分配量",
                 "note": e4, "html": pack_tbl},
                {"h": "证据⑤ 方法对照与交叉验证",
                 "note": "四条独立证据证明「必须先判队列、再算速度」。",
                 "html": e5},
                {"h": "证据⑥ 均衡化模拟：不加资源，把负载拉平",
                 "note": e6, "html": _bal_html_note(d)},
            ]),
        advice='<div class="findings">%s</div>' % "".join(adv),
        footer=footer, charts=charts,
        data_js=_json({"slots": slots, "mods": mods, "done": done, "util": util,
                       "queue": queue, "wLong": w_long, "wShort": w_short,
                       "peak": peak, "nominal": nominal, "cap": d["cap"],
                       "multiDay": multi_day, "nDays": n_days,
                       "bestDay": best_day_label,
                       "days": [{"label": x["label"], "total": x["total"],
                                 "speed": x.get("speed_long") or 0,
                                 "peak": x.get("peak") or 0}
                                for x in days_cmp]}),
    )


def mod_list_at(meta, i):
    return meta[i]


def _short_slot(s):
    """14:00–15:00 → 14-15 点（表格里省地方）。"""
    if not s:
        return "—"
    p = s.replace(":00", "").split("–")
    return "%s-%s点" % (p[0], p[1]) if len(p) == 2 else s


def _judge_html(m, is_hot):
    if m["ctrl"]:
        return "<span class='mute'>对照组·不作速度解读</span>"
    if is_hot:
        return "%s（%s 项目包）" % (
            kw("满转", "饱和窗内速度 ≥ 标称的 96%，说明整个测速窗口都在全力干活。"),
            esc(m["top"]))
    return "受分配量限制的%s" % kw(
        "表观速度", "不满负荷时段算出的「每小时完成数」，反映喂料节奏，不是机器能力上限。")


def _bal_target_txt(d):
    if not d.get("bal"):
        return "—"
    ps = [b["pct"] for b in d["bal"].values()]
    return "%.0f–%.0f%%" % (min(ps), max(ps))


def _cur_spread_txt(d, nominal):
    if not nominal:
        return "—"
    ns = [w for i, w in enumerate(d["w_long"])
          if not any(x["ctrl"] and x["key"] == d["mod_list"][i] for x in d["meta"])]
    if not ns:
        return "—"
    return "%.0f%%–%.0f%%" % (min(ns) * 100.0 / nominal, max(ns) * 100.0 / nominal)


def _bal_html_note(d):
    if not d.get("bal"):
        return ""
    cur = _cur_spread_txt(d, d["nominal"])
    tgt = _bal_target_txt(d)
    return ('<div class="aside">💡 <b>备注：</b>均衡化只是把总量在模块间<b>重新摊平</b>'
            '的模拟，不改变当天总工作量，也不需要新买设备 —— '
            '负载从 %s 拉平到 %s 只靠调整项目挂载。</div>' % (esc(cur), esc(tgt)))


def _stack_opt(mods, d):
    """证据④ 项目包堆叠条形图。"""
    # 收集各模块的项目分布，取全局 TOP 项目作为堆叠维度
    allp = {}
    for m in mods:
        for p, v in d["mod_proj"].get(m, {}).items():
            allp[p] = allp.get(p, 0) + v
    if not allp:
        return None
    tops = [p for p, _ in sorted(allp.items(), key=lambda kv: -kv[1])[:6]]
    palette = ["#e02020", "#f59a23", "#7b5cd6", "#13a8a8", "#2f7ce0", "#5DCAA5"]
    series = []
    for j, p in enumerate(tops):
        series.append({
            "name": p, "type": "bar", "stack": "pk", "barWidth": "54%",
            "itemStyle": {"color": palette[j % len(palette)]},
            "data": [d["mod_proj"].get(m, {}).get(p, 0) for m in mods],
        })
    rest = [sum(d["mod_proj"].get(m, {}).values())
            - sum(d["mod_proj"].get(m, {}).get(p, 0) for p in tops) for m in mods]
    if any(v > 0 for v in rest):
        series.append({"name": "其他", "type": "bar", "stack": "pk",
                       "barWidth": "54%", "itemStyle": {"color": "#B4B2A9"},
                       "data": rest})
    return {
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "legend": {"textStyle": {"fontSize": 11.5, "color": "#1f2d3d"}, "top": 0,
                   "type": "scroll"},
        "grid": {"left": 76, "right": 34, "top": 40, "bottom": 34},
        "xAxis": {"type": "value", "name": "测试数", **_AX,
                  "splitLine": {"lineStyle": {"color": "#eef2f6"}}},
        "yAxis": {"type": "category", "data": list(reversed(mods)), **_AX,
                  "axisLabel": {"fontSize": 10.5, "color": "#1f2d3d"}},
        "series": [{**s, "data": list(reversed(s["data"]))} for s in series],
    }


def _bal_opt(d, nominal):
    """证据⑥ 均衡化对比图。"""
    if not d.get("bal"):
        return None
    devs = []
    for k, b in d["bal"].items():
        for i, m in enumerate(d["mod_list"]):
            if m in d["mod_proj"] and not any(
                    x["ctrl"] and x["key"] == m for x in d["meta"]):
                devs.append((m, round(d["w_long"][i] * 100.0 / nominal, 1)
                             if nominal else 0, b["pct"]))
    if not devs:
        return None
    labels = [x[0] for x in devs]
    cur = [x[1] for x in devs]
    tgt = [x[2] for x in devs]
    return {
        "tooltip": {"trigger": "axis", "valueFormatter": "pfmt"},
        "legend": _LEG,
        "grid": {"left": 56, "right": 24, "top": 36, "bottom": 60},
        "xAxis": {"type": "category", "data": labels, **_AX,
                  "axisLabel": {"fontSize": 10.5, "color": "#5e6d82",
                                "interval": 0, "rotate": 22}},
        "yAxis": {"type": "value", "max": 115,
                  "axisLabel": {"formatter": "{value}%", "fontSize": 11,
                                "color": "#5e6d82"},
                  "splitLine": {"lineStyle": {"color": "#eef2f6"}}, **_AX},
        "series": [
            {"name": "当前负载%（饱和窗）", "type": "bar", "barWidth": "30%",
             "itemStyle": {"color": "#2f7ce0"},
             "label": {"show": True, "position": "top", "fontSize": 9,
                       "color": "#2f7ce0"}, "data": cur},
            {"name": "均衡化目标", "type": "bar", "barWidth": "30%",
             "itemStyle": {"color": "#07c160"},
             "label": {"show": True, "position": "top", "fontSize": 9,
                       "color": "#0a7a3d"}, "data": tgt},
            {"name": "100% 满负荷线", "type": "line", "symbol": "none",
             "tooltip": {"show": False},
             "lineStyle": {"type": "dashed", "color": "#e02020", "width": 1.5},
             "data": [100] * len(labels)},
        ],
    }


# ---------------------------------------------------------------------------
# 模板① 用的通用条形图
# ---------------------------------------------------------------------------

def _bar_h(items, color, unit):
    """横向条形图（TOP-N 项目）。items: [(name, val, pct)]。"""
    labels = [x[0] for x in items]
    vals = [x[1] for x in items]
    return {
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "grid": {"left": 96, "right": 56, "top": 12, "bottom": 30},
        "xAxis": {"type": "value", "name": unit, **_AX,
                  "splitLine": {"lineStyle": {"color": "#eef2f6"}}},
        "yAxis": {"type": "category", "data": list(reversed(labels)), **_AX,
                  "axisLabel": {"fontSize": 11, "color": "#1f2d3d"}},
        "series": [{"type": "bar", "barWidth": "55%",
                    "itemStyle": {"color": color, "borderRadius": [0, 4, 4, 0]},
                    "label": {"show": True, "position": "right", "fontSize": 10.5,
                              "color": "#5e6d82"},
                    "data": list(reversed(vals))}],
    }


_AX = {"axisLabel": {"fontSize": 11, "color": "#5e6d82"},
       "axisLine": {"lineStyle": {"color": "#c9d4e0"}}}
_LEG = {"textStyle": {"fontSize": 12, "color": "#1f2d3d"}, "top": 0}


def _json(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# 页面骨架
# ---------------------------------------------------------------------------

def _page(title, h1, sub, verdict, chain, sum_tbl, kpis, nkpi, sections,
          advice, footer, charts, data_js=""):
    """拼出完整单文件 HTML。"""
    # sections 里连续的两个 half 合并成 grid2 行
    body = []
    buf = []
    for s in sections:
        if s.get("half"):
            buf.append(s)
            if len(buf) == 2:
                body.append(_two_col(buf))
                buf = []
        else:
            if buf:
                body.append(_two_col(buf))
                buf = []
            body.append(_section(s))
    if buf:
        body.append(_two_col(buf))

    return (
        "<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n"
        "<meta charset=\"UTF-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">\n"
        "<title>%s</title>\n"
        "<script src=\"%s\"></script>\n"
        "<script>window.echarts||document.write('<script src=\"%s\"><\\/script>')</script>\n"
        "<style>%s</style>\n</head>\n<body>\n<div class=\"wrap\">\n"
        "<header><h1>%s</h1><div class=\"sub\">%s</div></header>\n"
        "<div class=\"verdict\">%s</div>\n"
        "<div class=\"chain\">%s</div>\n"
        "%s\n"
        "<div class=\"kpis n%d\">%s</div>\n"
        "%s\n"
        "<div class=\"card\"><h2><span class=\"no\">结论</span>可落地建议</h2>"
        "%s</div>\n"
        "<footer>%s</footer>\n"
        "</div>\n"
        "<script>\n(function(){\n"
        "if(typeof echarts==='undefined'){document.querySelectorAll('.chart')"
        ".forEach(function(e){e.innerHTML='<div style=\"padding:24px;text-align:center;"
        "color:#5e6d82;font-size:12.5px;line-height:1.9\">📉 图表未加载（无法访问 "
        "ECharts CDN）<br>表格与结论不受影响，可正常阅读。</div>';});return;}\n"
        "var DATA=%s;\n"
        "var VFMT={vfmt:function(v){return v+' /h';},pfmt:function(v){return v+'%%';}};\n"
        "var CHARTS=%s;\n"
        "function fix(o){\n"
        "  if(o===null||typeof o!=='object') return o;\n"
        "  Object.keys(o).forEach(function(k){\n"
        "    var v=o[k];\n"
        "    if(typeof v==='string'&&VFMT[v]) o[k]=VFMT[v];\n"
        "    else if(v&&typeof v==='object') fix(v);\n"
        "  });\n"
        "  return o;\n"
        "}\n"
        "var inst=[];\n"
        "CHARTS.forEach(function(c){\n"
        "  var el=document.getElementById(c.id); if(!el) return;\n"
        "  var opt=fix(c.opt)||{};\n"
        "  opt.tooltip=opt.tooltip||{};\n"
        "  var ch=echarts.init(el); ch.setOption(opt); inst.push(ch);\n"
        "});\n"
        "window.addEventListener('resize',function(){inst.forEach(function(c)"
        "{c.resize();});});\n"
        "})();\n</script>\n</body>\n</html>\n"
        % (esc(title), ECHARTS_CDN_1, ECHARTS_CDN_2, CSS, h1, sub, verdict, chain,
           sum_tbl, nkpi, kpis, "".join(body), advice, footer,
           data_js, _charts_json(charts)))


def _charts_json(charts):
    """图表配置序列化。函数占位符以字符串 'vfmt'/'pfmt' 形式落地，前端 fix() 还原。"""
    out = [{"id": c["id"], "opt": c["opt"]} for c in charts]
    return json.dumps(out, ensure_ascii=False, separators=(",", ":"))


def _section(s):
    cls = "card"
    parts = ['<div class="%s">' % cls]
    title = s["h"]
    # 把开头的 ①② 变成 .no 标签
    if title and title[0] in "①②③④⑤⑥⑦⑧⑨":
        parts.append('<h2><span class="no">%s</span>%s</h2>'
                     % (esc(title[0]), esc(title[1:].strip())))
    elif "证据" in title[:3]:
        idx = title.find(" ")
        if idx > 0:
            parts.append('<h2><span class="no">%s</span>%s</h2>'
                         % (esc(title[:idx]), esc(title[idx + 1:])))
        else:
            parts.append("<h2>%s</h2>" % esc(title))
    else:
        parts.append("<h2>%s</h2>" % esc(title))
    if s.get("note"):
        parts.append('<div class="note">%s</div>' % s["note"])
    for cid in s.get("charts", []):
        extra = ""
        if s.get("charts") and "ec3" in cid:
            extra = ' style="height:400px"'
        parts.append('<div id="%s" class="chart%s"%s></div>'
                     % (esc(cid), " tall" if _is_tall(cid, s) else "", extra))
    if s.get("html"):
        parts.append(s["html"])
    parts.append("</div>")
    return "".join(parts)


def _is_tall(cid, s):
    return cid in ("c1", "c4", "ec1", "ec2")


def _two_col(items):
    inner = "".join(_section(s) for s in items)
    return '<div class="grid2">%s</div>' % inner
