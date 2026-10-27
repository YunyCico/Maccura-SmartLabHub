"""Vue 外壳（工作平台）的导航与 LIS 集成静态回归。

这些断言锁住用户直接可见的行为：
返回主页按钮存在、路由正确、LIS 打开/关闭的消息契约不破。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "home" / "src"
DATA_ANALYSIS = FRONTEND / "components" / "DataAnalysis.vue"
STYLE = FRONTEND / "style.css"
ROUTER = FRONTEND / "router" / "index.ts"


def test_embed_toolbar_has_back_home_button():
    """数据分析页嵌入工具栏必须提供「返回 SmartLabHub」入口。

    回归背景：该按钮曾缺失，用户进数据分析后无法退回首页；
    后按用户截图要求改为与占位模块页一致的蓝色实心胶囊（.primary-button）。
    """
    vue = DATA_ANALYSIS.read_text(encoding="utf-8")
    assert 'class="primary-button back-home-button"' in vue, "返回按钮必须用主按钮样式"
    assert "返回 SmartLabHub" in vue
    assert "↗" in vue, "应带与占位模块页一致的箭头"
    assert '@click="backToHome"' in vue
    # 按钮必须排在「原窗口打开」「重新连接」之前，与其他操作同一组
    actions = vue[vue.index('class="embed-actions"'):vue.index("</div>", vue.index('class="embed-actions"'))]
    assert actions.index("back-home-button") < actions.index("原窗口打开")


def test_toolbar_has_single_primary_action():
    """工具栏只保留一个实心主按钮，避免两个蓝胶囊并排失去主次。"""
    vue = DATA_ANALYSIS.read_text(encoding="utf-8")
    actions = vue[vue.index('class="embed-actions"'):vue.index("</div>", vue.index('class="embed-actions"'))]
    assert actions.count("primary-button") == 1, "embed-actions 内只能有一个 primary-button"
    assert 'class="secondary-button" type="button" @click="loadEmbed"' in actions, "重新连接应为描边次级操作"


def test_back_home_closes_lis_overlay_before_navigating():
    """回主页前先撤掉 LIS 覆盖层，避免再次进入时仍停在 LIS 界面。"""
    vue = DATA_ANALYSIS.read_text(encoding="utf-8")
    body = vue[vue.index("function backToHome()"):vue.index("async function loadEmbed()")]
    assert "closeLisOrganizer()" in body, "返回主页前应先关闭 LIS 覆盖层"
    assert "router.push('/')" in body, "应跳转到主页路由"
    assert "useRouter" in vue


def test_home_route_exists():
    router = ROUTER.read_text(encoding="utf-8")
    assert "path: '/'" in router and "name: 'home'" in router
    assert "path: '/data-analysis'" in router


def test_back_home_reuses_platform_primary_button_style():
    """返回按钮必须复用平台既有的 .primary-button，不另造一套样式。"""
    vue = DATA_ANALYSIS.read_text(encoding="utf-8")
    planned = (FRONTEND / "views" / "PlannedModuleView.vue").read_text(encoding="utf-8")
    assert 'class="primary-button"' in planned, "占位模块页的主按钮基准样式"
    assert "返回 SmartLabHub" in planned and "返回 SmartLabHub" in vue, "两处文案应一致"
    css = STYLE.read_text(encoding="utf-8")
    assert ".back-home-button" not in css, "返回按钮应直接用平台主按钮样式，无需单独覆写"


def test_lis_close_message_contract_still_present():
    """LIS 打开/关闭消息契约不破：原生 iframe 靠它收起覆盖层。"""
    vue = DATA_ANALYSIS.read_text(encoding="utf-8")
    assert "smartlabhub:open-lis-organizer" in vue
    assert "smartlabhub:navigate" in vue
    assert "smartlabhub:close-lis-organizer" in vue
    assert "event.source !== embedFrame.value?.contentWindow" in vue, "必须校验消息来源 iframe"


def test_lis_sheet_picker_reloads_source_on_change():
    """切换工作表必须重新读取源表。

    回归背景：工作表下拉框原本只有 v-model、没有 @change。
    用户切了工作表后，界面显示的是新表名，但后端会话（暂存文件）还是旧表，
    字段对应表与真实来源对不上，确认预览时报「来源字段不存在：xxx」。
    """
    vue = (FRONTEND / "components" / "LisDataOrganizer.vue").read_text(encoding="utf-8")
    start = vue.index('v-model="selectedSheet"')
    picker = vue[start:vue.index("</select>", start)]
    assert '@change="previewImported"' in picker, "切换工作表后必须重新读取已导入表"
