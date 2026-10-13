"""TAT 与设备负载分析的前端隔离静态回归测试。"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "modules" / "data_analysis" / "web" / "app.js"
HTML = ROOT / "modules" / "data_analysis" / "web" / "index.html"


def test_generate_summary_stays_on_build_page():
    """「生成汇总表」后不得再跳转到数据分析页。

    跳转原本是为了展示已删除的「结果分析」面板；面板移除后，
    汇总结果就显示在汇总提取页自己的 #result-card 里。
    """
    js = APP.read_text(encoding="utf-8")
    run_union = js[js.index("async function runUnion()"):js.index("$('#btn-run').onclick = runUnion;")]
    assert "nav('analysis')" not in run_union, "生成汇总后不应再跳转页面"
    assert "showResult(r, S.outFields);" in run_union
    # 结果卡片必须位于汇总提取页（page-build）区块内，而不是数据分析页
    html = HTML.read_text(encoding="utf-8")
    build_start = html.index('id="page-build"')
    analysis_start = html.index('id="page-analysis"')
    result_at = html.index('id="result-card"')
    assert build_start < result_at, "#result-card 应在汇总提取页内"
    assert analysis_start < build_start, "数据分析页区块应在汇总提取页之前"


def test_removed_result_analysis_panel_is_gone():
    """「结果分析」面板与顶部灰色说明条已从页面移除。"""
    html = HTML.read_text(encoding="utf-8")
    for gone in ('id="analysis-card"', 'id="an-pick"', 'id="an-badge"',
                 'id="an-loading"', 'id="an-body"', 'id="an-kpis"', 'id="an-table"',
                 'id="pv-run"', 'id="pv-out"', 'id="pv-dims"'):
        assert gone not in html, f"{gone} 应已从页面移除"
    assert "结果分析使用" not in html
    assert "数据概览" not in html
    assert "数据透视" not in html
    # TAT 与设备负载必须保留
    for keep in ('id="tat-card"', 'id="tat-ds-list"', 'id="report-card"', 'id="rp-ds-list"'):
        assert keep in html, f"{keep} 必须保留"


def test_removed_panel_guards_do_not_throw():
    """元素删掉后，既有调用链不能因为找不到 DOM 而抛错。"""
    js = APP.read_text(encoding="utf-8")

    an_reset = js[js.index("function anReset()"):js.index("function anSwitchTab(")]
    assert "if (e)" in an_reset, "anReset 应对缺失元素做空值保护"

    an_switch = js[js.index("function anSwitchTab("):js.index("const __anTabP")]
    assert "if (!profile || !pivot) return;" in an_switch

    an_start = js[js.index("async function anStart("):js.index("function anPaintProfile(")]
    assert "if (!$('#analysis-card')) return;" in an_start

    run_pivot = js[js.index("async function anRunPivot()"):js.index("const __pvRun")]
    assert "if (!$('#pv-agg')) return;" in run_pivot

    # 顶层事件绑定必须改成带判断的写法
    assert "$('#pv-run').onclick = anRunPivot;" not in js
    assert "$('#pv-dim-add').onclick = () => {" not in js
    assert "$('#pv-agg').onchange = anSyncAgg;" not in js
    assert "const __pvRun = $('#pv-run'); if (__pvRun)" in js

    # 导航不再搬运已删除的面板
    nav = js[js.index("function nav(p)"):js.index("$$('.nav a').forEach")]
    assert "$('#analysis-card')" not in nav


def test_lis_sidebar_action_opens_vue_panel_without_fake_native_page():
    """LIS 左侧栏入口应通过受限消息打开外层 Vue 面板，而非切到不存在的原生页。"""
    html = HTML.read_text(encoding="utf-8")
    js = APP.read_text(encoding="utf-8")
    vue = (ROOT / "home" / "src" / "components" / "DataAnalysis.vue").read_text(encoding="utf-8")

    assert '<a data-action="lis-organizer"><i>▦</i>LIS 数据整理</a>' in html
    assert 'id="page-lis-organizer"' not in html
    assert "event.preventDefault();" in js
    assert "smartlabhub:open-lis-organizer" in js
    assert "smartlabhub:close-lis-organizer" in js
    nav = js[js.index("function nav(p)"):js.index("$$('.nav a').forEach", js.index("function nav(p)"))]
    assert "smartlabhub:navigate" in nav
    assert "smartlabhub:navigate" in vue
    assert "event.origin !== __parentOrigin" in js
    assert "event.source !== window.parent" in js
    assert "event.source !== embedFrame.value?.contentWindow" in vue
    assert '<main v-if="showLisOrganizer" class="lis-workspace"' in vue
    assert "<LisDataOrganizer />" in vue
    assert "返回数据分析" in vue
    assert "closeLisOrganizer" in vue
    assert ":class=\"{ 'is-lis-open': showLisOrganizer }\"" in vue


def test_analysis_has_independent_selectors_and_run_buttons():
    html = HTML.read_text(encoding="utf-8")
    for marker in (
        'id="tat-ds-list"',
        'id="tat-ds-run"',
        'id="rp-ds-list"',
        'id="rp-ds-run"',
        '不会影响设备负载分析',
        '不会影响 TAT 分析',
    ):
        assert marker in html
    assert 'id="an-ds-list"' not in html
    assert 'id="btn-an-run"' not in html


def test_analysis_state_and_result_paths_are_separate():
    js = APP.read_text(encoding="utf-8")
    assert "S.tatDs" in js and "S.rpDs" in js
    assert "S.tatResult" in js and "S.rpResult" in js
    assert "ensureTatResultFromSelection" in js
    assert "ensureReportResultFromSelection" in js
    assert "ensureResultFromSelection" not in js
    show_result = js[js.index("function showResult("):js.index("async function renderResultPage", js.index("function showResult("))]
    assert "rpStart(" not in show_result
    assert "tatFill(" not in show_result


def test_empty_dataset_state_does_not_auto_apply_history():
    """无数据表时历史结果不得被自动套用，避免进入页面后持续统计。"""
    js = APP.read_text(encoding="utf-8")
    assert "resetAnalysisStateWhenNoDatasets" in js
    picker = js[js.index("async function loadResultPicker"):js.index("async function applyPickedResult")]
    assert "if (!(S.datasets || []).length)" in picker
    assert "resetAnalysisStateWhenNoDatasets();" in picker
    apply_picker = js[js.index("async function applyPickedResult"):js.index("/* ★ 数据分析页")]
    assert "if (!rid || !(S.datasets || []).length) return;" in apply_picker
    assert "if (!S.lastResult && list.length) await applyPickedResult(cur);" in js


def test_startup_script_uses_its_own_project_directory():
    bat_raw = (ROOT / "启动平台.bat").read_bytes()
    assert all(byte < 128 for byte in bat_raw), "启动批处理必须保持纯 ASCII"
    bat = bat_raw.decode("ascii")
    assert 'cd /d "%~dp0"' in bat
    launcher = (ROOT / "platform" / "tools" / "launch_platform.py").read_text(encoding="utf-8")
    # 启动器在 platform/tools/ 下，项目根是上两层（tools -> platform -> 项目根）
    assert 'Path(__file__).resolve().parents[2]' in launcher
    assert "Documents\\Qoder\\2026-09-28\\58727fb4" not in bat
    assert "Documents\\Qoder\\2026-09-28\\58727fb4" not in launcher


def test_run_and_clear_do_not_share_the_other_analysis():
    js = APP.read_text(encoding="utf-8")
    tat_start = js.index("async function tatRun()")
    tat_end = js.index("function anReset()", tat_start)
    tat_body = js[tat_start:tat_end]
    assert "S.tatResult" in tat_body
    assert "ensureTatResultFromSelection" in tat_body
    assert "S.rpResult" not in tat_body

    rp_start = js.index("async function rpRun()")
    rp_end = js.index("const RP_KPI_LABEL", rp_start)
    rp_body = js[rp_start:rp_end]
    assert "S.rpResult.id" in rp_body
    assert "ensureReportResultFromSelection" in rp_body
    assert "S.tatResult" not in rp_body

    tat_clear = js[js.index("function tatClear()"):js.index("async function tatRun()")]
    assert "S.tatResult = null" in tat_clear
    assert "S.rpResult = null" not in tat_clear
