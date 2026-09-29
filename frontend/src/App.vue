<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { fetchDashboardSummary, type DashboardSummary } from './api/dashboard'
import { fetchCurrentUser, type CurrentUser } from './api/user'
import DataAnalysis from './components/DataAnalysis.vue'

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
  { label: '数据分析', code: '02' },
  { label: '钉钉听记内容标准化处理', code: '03' },
  { label: '6S标准化报告', code: '04' },
]

const workspaceModules = [
  {
    code: '01',
    label: '数据分析',
    section: '数据分析',
    description: '原样嵌入 SmartLabHub 数据汇总助手 v2.4.0：概览、数据源、字段字典、汇总提取、横向关联、结果分析、报告、导出、AI、设置、钉钉入口和原始编辑。',
    status: '可用',
    action: '进入数据分析',
  },
  {
    code: '02',
    label: '钉钉听记内容标准化处理',
    section: '钉钉听记内容标准化处理',
    description: '预留钉钉听记文本清洗、要点抽取、标准字段整理和报告输出能力。',
    status: '规划中',
    action: '查看模块',
  },
  {
    code: '03',
    label: '6S标准化报告',
    section: '6S标准化报告',
    description: '预留 6S 检查记录、问题归集、整改跟踪和标准化报告生成能力。',
    status: '规划中',
    action: '查看模块',
  },
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

        <section class="module-panel panel">
          <div class="panel-heading">
            <div>
              <p class="section-kicker">WORKSPACE MODULES</p>
              <h3>部门工作模块</h3>
            </div>
            <span class="module-count">3 个模块</span>
          </div>
          <div class="module-grid">
            <button
              v-for="module in workspaceModules"
              :key="module.code"
              class="module-card"
              :class="{ planned: module.status === '规划中' }"
              type="button"
              @click="activeSection = module.section"
            >
              <span class="module-code">{{ module.code }}</span>
              <strong>{{ module.label }}</strong>
              <p>{{ module.description }}</p>
              <span class="module-footer">
                <span class="module-status" :class="{ ready: module.status === '可用' }">{{ module.status }}</span>
                <span class="module-action">{{ module.action }} ↗</span>
              </span>
            </button>
          </div>
        </section>

        <div class="lower-grid">
          <section class="panel task-panel">
            <div class="panel-heading">
              <div>
                <p class="section-kicker">PROCESS QUEUE</p>
                <h3>最近处理任务</h3>
              </div>
              <button class="text-button" type="button" @click="activeSection = '数据分析'">查看全部 <span>↗</span></button>
            </div>
            <div v-if="summary.recent_tasks.length === 0" class="empty-state">
              <div class="empty-mark">—</div>
              <strong>还没有处理记录</strong>
              <p>上传第一份 Excel 后，处理批次会显示在这里。</p>
              <button class="primary-button" type="button" @click="activeSection = '数据分析'">开始导入 <span>↗</span></button>
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

      <DataAnalysis v-if="activeSection === '数据分析'" />

      <section v-else-if="activeSection !== '工作台'" class="placeholder-view">
        <span class="placeholder-code">MODULE / {{ navigation.find((item) => item.label === activeSection)?.code }}</span>
        <h2>{{ activeSection }}</h2>
        <p v-if="activeSection === '钉钉听记内容标准化处理'">该模块用于处理钉钉听记文本：清洗口语内容、抽取关键事项、统一人员/时间/问题/结论字段，并生成可归档的标准化记录。当前功能尚未开发完成，先保留入口。</p>
        <p v-else-if="activeSection === '6S标准化报告'">该模块用于整理 6S 检查记录、问题分类、整改跟踪和现场照片说明，并生成标准化报告。当前功能尚未开发完成，先保留入口。</p>
        <p v-else>当前已完成平台壳层、后端接口和本地用户模拟。下一步将接入该模块的业务流程。</p>
        <button class="primary-button" type="button" @click="activeSection = '工作台'">返回工作台 <span>↗</span></button>
      </section>
    </main>
  </div>
</template>
