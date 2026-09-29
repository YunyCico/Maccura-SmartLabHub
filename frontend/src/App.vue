<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { fetchDashboardSummary, type DashboardSummary } from './api/dashboard'
import { fetchCurrentUser, type CurrentUser } from './api/user'
import ImportPreview from './components/ImportPreview.vue'

const activeSection = ref('工作台')
const loading = ref(true)
const apiError = ref('')
const user = ref<CurrentUser | null>(null)
const summary = ref<DashboardSummary>({
  service_request_count: 0,
  completed_count: 0,
  average_satisfaction: '0.00',
  pending_import_count: 0,
  recent_tasks: [],
})

const navigation = [
  { label: '工作台', code: '01' },
  { label: '数据导入', code: '02' },
  { label: '服务需求', code: '03' },
  { label: '分析看板', code: '04' },
  { label: '文件导出', code: '05' },
]

async function loadWorkspace() {
  loading.value = true
  apiError.value = ''

  try {
    const [currentUser, dashboard] = await Promise.all([
      fetchCurrentUser(),
      fetchDashboardSummary(),
    ])
    user.value = currentUser
    summary.value = dashboard
  } catch (error) {
    apiError.value = '暂时无法连接后端服务，当前显示本地空状态。'
    console.error(error)
  } finally {
    loading.value = false
  }
}

onMounted(loadWorkspace)
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand-lockup">
        <div class="brand-mark" aria-hidden="true">SL</div>
        <div>
          <p class="brand-name">SMARTLAB</p>
          <p class="brand-subtitle">运营管理平台</p>
        </div>
      </div>

      <div class="sidebar-rule"></div>
      <p class="nav-caption">部门工作区</p>
      <nav class="main-nav" aria-label="主导航">
        <button
          v-for="item in navigation"
          :key="item.label"
          class="nav-item"
          :class="{ active: activeSection === item.label }"
          type="button"
          @click="activeSection = item.label"
        >
          <span class="nav-code">{{ item.code }}</span>
          <span>{{ item.label }}</span>
          <span v-if="activeSection === item.label" class="nav-arrow" aria-hidden="true">↗</span>
        </button>
      </nav>

      <div class="sidebar-bottom">
        <div class="status-line"><span class="status-dot"></span>本地开发环境</div>
        <p>钉钉挂载版本 · 预备中</p>
      </div>
    </aside>

    <main class="workspace">
      <header class="topbar">
        <div>
          <p class="eyebrow">OPERATIONS CONTROL ROOM / 2026.09</p>
          <h1>{{ activeSection }}</h1>
        </div>
        <div class="user-block">
          <div class="user-copy">
            <span>{{ user?.name ?? '本地管理员' }}</span>
            <small>{{ user?.role ?? 'department_admin' }}</small>
          </div>
          <div class="avatar">管</div>
        </div>
      </header>

      <section v-if="activeSection === '工作台'" class="dashboard-content">
        <div class="intro-row">
          <div>
            <p class="section-kicker">今日工作概览</p>
            <h2>把数据整理，变成可执行的判断。</h2>
            <p class="intro-copy">集中处理医院上报、服务需求与运营指标，先确认数据，再生成结论。</p>
          </div>
          <div class="date-stamp">
            <span class="date-label">工作区状态</span>
            <strong>{{ loading ? '同步中' : '已就绪' }}</strong>
            <span class="date-line"></span>
          </div>
        </div>

        <div v-if="apiError" class="notice-bar warning"><span class="notice-mark">!</span>{{ apiError }}</div>

        <div class="metrics-grid">
          <article class="metric-card metric-primary">
            <div class="metric-topline"><span>服务需求</span><span class="metric-index">01</span></div>
            <strong>{{ summary.service_request_count }}</strong>
            <p>当前周期累计记录</p>
          </article>
          <article class="metric-card">
            <div class="metric-topline"><span>已完成</span><span class="metric-index">02</span></div>
            <strong>{{ summary.completed_count }}</strong>
            <p>已完成处理的需求</p>
          </article>
          <article class="metric-card metric-accent">
            <div class="metric-topline"><span>平均满意度</span><span class="metric-index">03</span></div>
            <strong>{{ summary.average_satisfaction }}</strong>
            <p>统一保留两位小数</p>
          </article>
          <article class="metric-card">
            <div class="metric-topline"><span>待确认批次</span><span class="metric-index">04</span></div>
            <strong>{{ summary.pending_import_count }}</strong>
            <p>等待人工确认的数据</p>
          </article>
        </div>

        <div class="lower-grid">
          <section class="panel task-panel">
            <div class="panel-heading">
              <div>
                <p class="section-kicker">PROCESS QUEUE</p>
                <h3>最近处理任务</h3>
              </div>
              <button class="text-button" type="button" @click="activeSection = '数据导入'">查看全部 <span>↗</span></button>
            </div>
            <div v-if="summary.recent_tasks.length === 0" class="empty-state">
              <div class="empty-mark">—</div>
              <strong>还没有处理记录</strong>
              <p>上传第一份 Excel 后，处理批次会显示在这里。</p>
              <button class="primary-button" type="button" @click="activeSection = '数据导入'">开始导入 <span>↗</span></button>
            </div>
            <div v-else class="task-list">
              <div v-for="task in summary.recent_tasks" :key="String(task.id)" class="task-row">
                <span class="task-type">XLS</span>
                <div class="task-copy"><strong>{{ task.filename }}</strong><p>{{ task.rows }} 行 · {{ task.sheets }} 个工作表</p></div>
                <span class="task-status">{{ task.status }}</span>
              </div>
            </div>
          </section>

          <section class="panel quality-panel">
            <div class="panel-heading">
              <div>
                <p class="section-kicker">DATA QUALITY</p>
                <h3>数据质量提醒</h3>
              </div>
              <span class="quality-count">{{ summary.pending_import_count }} 项</span>
            </div>
            <div class="quality-item"><span class="quality-icon neutral">01</span><div><strong>待导入文件</strong><p>等待下一批医院月度数据</p></div><span class="quality-state">空闲</span></div>
            <div class="quality-item"><span class="quality-icon neutral">02</span><div><strong>字段映射</strong><p>统一处理多层表头与 Unnamed 列</p></div><span class="quality-state">规则</span></div>
            <div class="quality-item"><span class="quality-icon accent">03</span><div><strong>人工确认</strong><p>正式写入前保留用户确认节点</p></div><span class="quality-state accent-text">必需</span></div>
          </section>
        </div>
      </section>

      <ImportPreview v-if="activeSection === '数据导入'" />

      <section v-else-if="activeSection !== '工作台'" class="placeholder-view">
        <span class="placeholder-code">MODULE / {{ navigation.find((item) => item.label === activeSection)?.code }}</span>
        <h2>{{ activeSection }}模块正在搭建</h2>
        <p>当前已完成平台壳层、后端接口和本地用户模拟。下一步将接入该模块的业务流程。</p>
        <button class="primary-button" type="button" @click="activeSection = '工作台'">返回工作台 <span>↗</span></button>
      </section>
    </main>
  </div>
</template>
