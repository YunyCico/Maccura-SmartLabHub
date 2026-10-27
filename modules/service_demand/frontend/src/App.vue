<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { api } from './api.js'
import ConfigPanel from './components/ConfigPanel.vue'
import PullForm from './components/PullForm.vue'
import ResultView from './components/ResultView.vue'

const conn = ref({ state: 'idle', text: '未检测', detail: '' })
const busy = ref(false)
const result = ref(null)
const history = ref([])
const activeStep = ref(0)
const apiBase = ref('')
const operators = ref([])
const tableFields = ref({})

/* 被 SmartLabHub 用 iframe 嵌进来时，顶栏多两个「平台导航」按钮。
   独立运行时（双击 启动控制台.bat）不显示 —— 那时候没有"回工作台"可回。 */
const embedded = computed(() => {
  try {
    return window.parent !== window
  } catch {
    return false
  }
})

function goHome() {
  try {
    // 同源，可以直接操作顶层窗口
    window.top.location.href = '/'
  } catch {
    window.location.href = '/'
  }
}

function reloadSelf() {
  window.location.reload()
}

// 右侧结果区是独立滚动容器，导航跳转要滚它而不是 window
function scrollStage(target) {
  const stage = document.querySelector('.stage')
  if (!stage) return
  if (target === 0) { stage.scrollTop = 0; return }
  const el = document.getElementById(target)
  if (!el) return
  // 目标面板若处于折叠态，先展开再滚，否则只会看到一行标题
  if (el.classList.contains('panel-collapsible') && !el.classList.contains('open')) {
    el.querySelector('.panel-head-toggle')?.click()
  }
  // 等一帧让 v-show 生效，拿到真实高度再定位
  requestAnimationFrame(() => {
    stage.scrollTop = el.offsetTop - 24
  })
}

const pullForm = reactive({
  username: '', busDomain: [], tableName: ['ADS_SERVICE_DEMAND_FEEDBACK'],
  metricsDomain: [], metricsName: [],
  combinationDomain: [], combinationName: [],
  condition: {}, pageNo: null, pageSize: null
})

const steps = computed(() => [
  { label: '提交参数', done: activeStep.value >= 1, active: activeStep.value === 0 },
  { label: '获取链接', done: activeStep.value >= 2, active: activeStep.value === 1 },
  { label: '下载文件', done: activeStep.value >= 3, active: activeStep.value === 2 },
  { label: '解析展示', done: activeStep.value >= 4, active: activeStep.value === 3 }
])

function setStep(n) { activeStep.value = n }

async function check() {
  conn.value = { state: 'loading', text: '检测中…', detail: '' }
  try {
    const d = await api.check()
    conn.value = {
      state: 'on',
      text: `已连接 · ${d.username}`,
      detail: `已完成身份验证｜授权状态正常｜${d.tokenCached ? '复用连接' : '建立连接'}`
    }
    if (!pullForm.username) pullForm.username = d.username || ''
  } catch (e) {
    conn.value = { state: 'off', text: '待配置', detail: e.message }
  }
}

async function loadHistory() {
  try { history.value = await api.history() } catch { history.value = [] }
}

async function runPull(silent = false) {
  busy.value = true
  try {
    setStep(0)
    const p = { username: pullForm.username }
    for (const k of ['busDomain', 'tableName', 'metricsDomain', 'metricsName',
                     'combinationDomain', 'combinationName']) {
      if (pullForm[k]?.length) p[k] = pullForm[k]
    }
    if (pullForm.pageNo && pullForm.pageSize) {
      p.pageNo = pullForm.pageNo
      p.pageSize = pullForm.pageSize
    }
    const cond = {}
    for (const [t, rows] of Object.entries(pullForm.condition || {})) {
      const valid = rows.filter((r) => r.field?.trim())
      if (valid.length) {
        cond[t] = valid.map((r) => {
          const item = { field: r.field.trim(), operator: r.operator }
          if (r.operator !== 'isNull' && r.operator !== 'isNotNull') {
            item.value = r.operator === 'in'
              ? String(r.value).split(',').map((s) => s.trim()).filter(Boolean)
              : r.value
          }
          return item
        })
      }
    }
    if (Object.keys(cond).length) p.condition = cond

    setStep(1)
    result.value = await api.pull(p)
    setStep(4)
    await loadHistory()
    // 拉取成功后刷新字段元数据（后端此时已缓存真实表结构）
    try {
      const m = await api.meta()
      operators.value = m.operators
      tableFields.value = m.tableFields || {}
    } catch { /* 忽略 */ }
  } catch (e) {
    result.value = { error: e.message }
    setStep(0)
  } finally { busy.value = false }
}

async function runSample() {
  busy.value = true
  try {
    setStep(1)
    result.value = await api.pullSample()
    setStep(4)
    await loadHistory()
  } catch (e) {
    result.value = { error: e.message }
    setStep(0)
  } finally { busy.value = false }
}

onMounted(async () => {
  try {
    const c = await api.getConfig()
    pullForm.username = c.username || ''
    apiBase.value = c.apiBase
    const m = await api.meta()
    operators.value = m.operators
    tableFields.value = m.tableFields || {}
  } catch (e) { /* 首屏失败不阻断 */ }
  await check()
  await loadHistory()
  // 进入平台即自动拉取固定数据表，无需业务人员手动点按钮
  if (conn.value.state === 'on') await runPull()
})
</script>

<template>
  <div class="shell">
    <section class="hero">
      <p class="hero-kicker">DataFetch Portal</p>
      <h2 class="hero-title">服务需求数据，<span class="hero-gradient">打开即得。</span></h2>
      <p class="hero-sub">进入平台即自动获取「服务需求反馈」数据——分类、客户、大区、满意度一屏看清。</p>
    </section>

    <div class="body">
      <aside class="sidebar">
        <!-- 原顶栏左侧的品牌与页内导航挪到这里：
             顶栏整条去掉后，页面顶部不再多占一行 -->
        <div class="side-head">
          <div class="side-brand">
            <div class="brand-mark">
              <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
              <path d="M16.36 12.78c.02 2.6 2.28 3.47 2.3 3.48-.02.06-.36 1.24-1.2 2.46-.72 1.05-1.47 2.1-2.66 2.12-1.16.02-1.54-.69-2.87-.69-1.33 0-1.75.67-2.85.71-1.14.04-2.01-1.13-2.74-2.18-1.5-2.17-2.64-6.14-1.1-8.82.76-1.33 2.13-2.17 3.61-2.19 1.12-.02 2.17.75 2.87.75.69 0 1.98-.93 3.33-.79.57.02 2.17.21 3.2 1.6-.08.05-1.91 1.12-1.89 3.55M14.3 4.9c.61-.74 1.02-1.77.91-2.8-.88.04-1.94.59-2.57 1.32-.56.65-1.05 1.69-.92 2.69.98.08 1.97-.5 2.58-1.21"/>
            </svg>
            </div>
            <div class="side-brand-text">
              <strong>数据中台</strong>
              <span>Xenon 数据中台 · 业务数据查询</span>
            </div>
          </div>
          <nav class="side-tabs">
            <a href="#stage" @click.prevent="scrollStage(0)">数据拉取</a>
            <a href="#bi-dashboard" @click.prevent="scrollStage('bi-dashboard')">BI 看板</a>
          </nav>
        </div>

        <ConfigPanel
          :conn="conn" :username="pullForm.username" :api-base="apiBase" @saved="check" />

        <div class="panel">
          <div class="panel-head">
            <h2>获取数据</h2>
            <span class="sub">按需添加筛选条件即可</span>
          </div>
          <div class="panel-body">
            <PullForm
              :form="pullForm" :busy="busy" :operators="operators" :table-fields="tableFields"
              @submit="runPull" @sample="runSample" />
          </div>
        </div>
      </aside>

      <main class="stage" id="stage">
        <!-- 原顶栏右侧的操作组挪到内容区右上角；sticky 让它滚动时也够得着 -->
        <div class="stage-actions">
          <div v-if="embedded" class="embed-actions">
            <button class="btn sample-button" @click="goHome">← 回工作台</button>
            <button class="btn sample-button" @click="reloadSelf">重新加载</button>
          </div>
          <div class="conn">
            <span class="dot" :class="conn.state"></span><span>{{ conn.text }}</span>
          </div>
          <button class="btn" @click="check" :disabled="conn.state === 'loading'">
            <span v-if="conn.state === 'loading'" class="spin"></span>
            {{ conn.state === 'loading' ? '检测中' : '连接检测' }}
          </button>
          <button class="btn btn-primary" @click="runPull" :disabled="busy">
            {{ busy ? '获取中' : '重新获取' }}
          </button>
        </div>

        <ResultView :result="result" :busy="busy" :history="history" @refresh="runPull" />
      </main>
    </div>
  </div>
</template>