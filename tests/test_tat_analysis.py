"""TAT 分析的回归测试：纯函数、只读、无任何落盘。"""
from smartlab_core import smartlab_engine as engine

COLS = ["模块", "开始时间", "结束时间", "备注"]
ROWS = [
    ["M1", "2026/09/19 08:00:00", "2026/09/19 09:30:00", "a"],   # 90min
    ["M1", "2026/09/19 08:00:00", "2026/09/19 12:00:00", "b"],   # 240min
    ["M2", "2026/09/19 08:00:00", "2026/09/20 08:00:00", "c"],   # 1440min
    ["M2", "2026/09/19 08:00:00", "bad-time", "d"],              # 解析失败
    ["M2", "2026/09/19 08:00:00", "2026/09/18 08:00:00", "e"],   # 负值
]


def test_tat_basic_stats_and_groups():
    r = engine.tat_analyze(COLS, ROWS, "开始时间", "结束时间", "模块")
    st = r["stats"]
    assert st["total"] == 5 and st["valid"] == 3 and st["invalid"] == 2
    assert st["avg_h"] == round((90 + 240 + 1440) / 3 / 60, 2)
    assert st["min_h"] == 1.5 and st["max_h"] == 24.0
    g = {x["name"]: x for x in r["groups"]}
    assert set(g) == {"M1", "M2"}
    assert g["M1"]["n"] == 2 and g["M2"]["n"] == 1
    assert sum(d["count"] for d in r["dist"]) == 3


def test_tat_rejects_bad_columns():
    for bad in [("不存在", "结束时间"), ("开始时间", "开始时间")]:
        try:
            engine.tat_analyze(COLS, ROWS, *bad)
            raise AssertionError("应当抛出 ValueError")
        except ValueError:
            pass


def test_tat_no_group_single_bucket_set():
    r = engine.tat_analyze(COLS, ROWS[:3], "开始时间", "结束时间", "")
    assert [d["count"] for d in r["dist"]] == [0, 1, 0, 1, 0, 1, 0]
    assert r["groups"] == [{"name": "未分组", "n": 3,
                            "avg_h": r["stats"]["avg_h"],
                            "median_h": r["stats"]["median_h"],
                            "p90_h": r["stats"]["p90_h"],
                            "max_h": r["stats"]["max_h"]}]
