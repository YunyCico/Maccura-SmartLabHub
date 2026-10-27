"""TAT 分段 / 环节识别 / 实验室 TAT 标准 / 超标判定的回归测试。

★ 2026-10-26 梦云要的：
  1. 计算时直接生成 采集-签收 / 签收-核收 / 核收-报告 / 报告-审核 四个分段，
     并同时给出实验室内 TAT 与 ToTAT；
  2. 参数区只显示"本次计算用到的字段"；
  3. 可设置实验室 TAT 标准（生化/免疫/血常规/凝血 × 平诊/急诊），
     分析后按标准判断哪些超出规定。
"""
import os
import tempfile

import pytest

from modules.data_analysis import smartlab_engine as engine
from modules.data_analysis import tat_rules as TR

# 一条标本走完全程：采集 08:00 → 签收 08:30 → 核收 09:00 → 报告 09:20 → 审核 09:40
COLS = ["专业", "就诊类型", "检验小组",
        "采集时间", "接收时间", "上机时间", "报告时间", "审核时间",
        "采样时间"]
ROWS = [
    ["生化", "门诊", "生化组",
     "2026/09/19 08:00:00", "2026/09/19 08:30:00", "2026/09/19 09:00:00",
     "2026/09/19 09:20:00", "2026/09/19 09:40:00", "2026/09/19 08:00:00"],
    ["生化", "急诊", "生化组",
     "2026/09/19 08:00:00", "2026/09/19 08:20:00", "2026/09/19 09:00:00",
     "2026/09/19 09:20:00", "2026/09/19 09:40:00", "2026/09/19 08:00:00"],
    ["免疫", "门诊", "免疫组",
     "2026/09/19 08:00:00", "2026/09/19 08:30:00", "2026/09/19 09:00:00",
     "2026/09/19 09:20:00", "2026/09/19 09:40:00", "2026/09/19 08:00:00"],
    ["血常规", "门诊", "血常规组",
     "2026/09/19 08:00:00", "2026/09/19 08:30:00", "2026/09/19 09:00:00",
     "2026/09/19 09:20:00", "2026/09/19 09:40:00", "2026/09/19 08:00:00"],
    # 生化门诊第二例：走得快（实验室内只有 20 分钟）→ 不超标，用来验超标率
    ["生化", "门诊", "生化组",
     "2026/09/19 08:00:00", "2026/09/19 08:25:00", "2026/09/19 08:30:00",
     "2026/09/19 08:35:00", "2026/09/19 08:40:00", "2026/09/19 08:00:00"],
    # 时间缺失 / 乱序 的行：各指标应分别判定有效性
    ["凝血", "门诊", "凝血组",
     "bad-time", "2026/09/19 08:30:00", "2026/09/19 09:00:00",
     "2026/09/19 09:20:00", "2026/09/19 09:40:00", "2026/09/19 08:00:00"],
]


# ---------------------------------------------------------------- 环节识别
def test_detect_stages_finds_all_five():
    d = TR.detect_stages(COLS)
    assert d["stages"] == {
        "采集": "采集时间", "签收": "接收时间", "核收": "上机时间",
        "报告": "报告时间", "审核": "审核时间",
    }
    assert d["missing"] == []


def test_detect_stages_reports_missing():
    d = TR.detect_stages(["条码号", "姓名", "采样时间", "审核时间"])
    assert d["stages"].get("采集") == "采样时间"
    assert d["stages"].get("审核") == "审核时间"
    assert "核收" in d["missing"] and "报告" in d["missing"]


def test_detect_stages_does_not_mix_report_and_audit():
    """「报告审核时间」不能被当成两个环节。"""
    d = TR.detect_stages(["报告时间", "审核时间", "报告审核时间"])
    assert d["stages"].get("报告") in ("报告时间", "报告审核时间")
    assert d["stages"].get("审核") == "审核时间"
    assert len(set(d["stages"].values())) == len(d["stages"]), "一列不能被两个环节占用"


# ---------------------------------------------------------------- 分段计算
def test_tat_segments_and_lab_tat():
    r = engine.tat_analyze(COLS, ROWS, "接收时间", "审核时间", "检验小组")
    names = [s["name"] for s in r["segments"]]
    assert names == ["采集-签收", "签收-核收", "核收-报告", "报告-审核"]
    seg = {s["name"]: s for s in r["segments"]}
    # 各段的起止列必须是自己那一段的（不是都拿审核时间当终点）
    assert seg["采集-签收"]["start"] == "采集时间" and seg["采集-签收"]["end"] == "接收时间"
    assert seg["签收-核收"]["start"] == "接收时间" and seg["签收-核收"]["end"] == "上机时间"
    assert seg["核收-报告"]["start"] == "上机时间" and seg["核收-报告"]["end"] == "报告时间"
    assert seg["报告-审核"]["start"] == "报告时间" and seg["报告-审核"]["end"] == "审核时间"
    # 采集-签收：30/20/30/30/25 分钟 → 平均 27 分钟 ≈ 0.45 小时
    st = seg["采集-签收"]["stats"]
    assert st["valid"] == 5
    assert abs(st["avg_h"] - 0.45) < 0.01, st["avg_h"]
    # 核收-报告：20/20/20/20/5 分钟 → 平均 17 分钟 ≈ 0.28 小时
    st = seg["核收-报告"]["stats"]
    assert abs(st["avg_h"] - 0.28) < 0.01, st["avg_h"]
    # 实验室内 TAT = 签收 → 审核
    assert r["lab_tat"]["start"] == "接收时间"
    assert r["lab_tat"]["end"] == "审核时间"
    assert r["lab_tat"]["stats"]["valid"] == 6
    # ToTAT = 采集 → 审核
    assert r["tot"]["start"] == "采样时间"
    assert r["tot"]["end"] == "审核时间"


def test_tat_segment_missing_column_is_skipped():
    r = engine.tat_analyze(["接收时间", "审核时间"], [ROWS[0][:0] or ["x", "x"]],
                           "接收时间", "审核时间")
    # 没有采集/核收/报告列 → 只有能凑齐的段
    assert [s["name"] for s in r["segments"]] == []


def test_tat_old_behaviour_kept():
    """不传 stages 时，主指标仍然是 start→end（旧调用不受影响）。"""
    r = engine.tat_analyze(COLS, ROWS, "接收时间", "审核时间", "检验小组")
    assert r["start"] == "接收时间" and r["end"] == "审核时间"
    assert r["stats"]["valid"] == 6


# ---------------------------------------------------------------- 标准配置
def test_standards_save_and_load_roundtrip():
    lib = tempfile.mkdtemp(prefix="slh_std_")
    data = TR.save_standards(lib, {"limits": {"生化": {"平诊": 4, "急诊": 1.5}},
                                  "target": "lab_tat"})
    assert data["limits"]["生化"]["平诊"] == 4
    assert data["limits"]["生化"]["急诊"] == 1.5
    assert data["limits"]["免疫"]["平诊"] is None      # 没设的留空
    back = TR.load_standards(lib)
    assert back["limits"]["生化"]["平诊"] == 4
    assert back["target"] == "lab_tat"


def test_standards_file_missing_returns_default():
    lib = tempfile.mkdtemp(prefix="slh_std_")
    data = TR.load_standards(lib)
    assert data["specialties"] == ["生化", "免疫", "血常规", "凝血"]
    # ★ 出厂默认口径（梦云 2026-10-27 按图定）：生化 4/2、免疫 4/2、血常规 0.5/0.25、凝血 2/1
    assert data["limits"]["生化"] == {"平诊": 4.0, "急诊": 2.0, "单位": "小时"}
    assert data["limits"]["免疫"] == {"平诊": 4.0, "急诊": 2.0, "单位": "小时"}
    assert data["limits"]["血常规"] == {"平诊": 0.5, "急诊": 0.25, "单位": "小时"}
    assert data["limits"]["凝血"] == {"平诊": 2.0, "急诊": 1.0, "单位": "小时"}
    assert data["target"] == "lab_tat"


def test_default_standards_are_the_agreed_ones():
    """默认值单独断言一次，防止以后被改回去。"""
    limits = TR.default_standards()["limits"]
    assert [(limits[s]["平诊"], limits[s]["急诊"]) for s in TR.STANDARD_SPECIALTIES] == [
        (4.0, 2.0), (4.0, 2.0), (0.5, 0.25), (2.0, 1.0),
    ]


# ---------------------------------------------------------------- 超标判定
def test_evaluate_marks_over_limit():
    standards = {"limits": {"生化": {"平诊": 0.5, "急诊": 10}}, "target": "lab_tat"}
    rows = [
        {"specialty": "生化", "urgency": "平诊", "value": 1.0},   # 超
        {"specialty": "生化", "urgency": "平诊", "value": 0.2},   # 不超
        {"specialty": "生化", "urgency": "急诊", "value": 5.0},   # 不超（限 10）
    ]
    out = TR.evaluate(rows, standards)
    g = {(x["specialty"], x["urgency"]): x for x in out["groups"]}
    assert g[("生化", "平诊")]["over"] == 1
    assert g[("生化", "平诊")]["judged"] == 2
    assert g[("生化", "平诊")]["over_pct"] == 50.0
    assert g[("生化", "急诊")]["over"] == 0
    assert out["summary"]["over"] == 1
    assert out["summary"]["judged"] == 3


def test_classify_specialty_and_urgency():
    assert TR.classify_specialty("生化") == "生化"
    assert TR.classify_specialty("临检血常规") == "血常规"
    assert TR.classify_specialty("免疫发光") == "免疫"
    assert TR.classify_specialty("凝血") == "凝血"
    assert TR.classify_specialty("分子呼吸道") == ""
    assert TR.classify_urgency("急诊") == "急诊"
    assert TR.classify_urgency("门诊") == "平诊"
    assert TR.classify_urgency("住院") == "平诊"


def test_tat_standard_check_end_to_end():
    """整套：识别环节 → 算分段 → 按标准判超标。"""
    standards = {"limits": {"生化": {"平诊": 0.5, "急诊": 100}}, "target": "lab_tat"}
    r = engine.tat_analyze(COLS, ROWS, "接收时间", "审核时间", "检验小组",
                           standards=standards)
    sd = r["standard"]
    assert sd, "应返回标准判定"
    g = {(x["specialty"], x["urgency"]): x for x in sd["groups"]}
    # 生化门诊两例：1.17h 那例超（限 0.5），0.25h 那例不超 → 超标率 50%
    assert g[("生化", "平诊")]["over"] == 1
    assert g[("生化", "平诊")]["judged"] == 2
    assert g[("生化", "平诊")]["over_pct"] == 50.0
    assert g[("生化", "急诊")]["over"] == 0
    assert sd["dims"]["specialty_col"] == "专业"
    assert sd["dims"]["urgency_col"] == "就诊类型"
