<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { fetchDashboardSummary, type DashboardSummary } from '../api/dashboard'

const router = useRouter()

const loading = ref(true)
const apiError = ref('')
const summary = ref<DashboardSummary>({
  service_request_count: 0,
  completed_count: 0,
  average_satisfaction: '0.00',
  pending_import_count: 0,
  recent_tasks: [],
})

const modules = [
  {
    code: '01',
    label: '数据分析',
    to: '/data-analysis',
    description: '原样迁入 SmartLabHub 数据汇总助手 v2.4.0：数据源、字段字典、汇总提取、横向关联、结果分析、报告与导出。',
    status: '可用',
    action: '进入数据分析',
    tone: 'blue',
  },
  {
    code: '02',
    label: '钉钉听记内容标准化处理',
    to: '/dingtalk-minutes',
    description: '预留钉钉听记文本清洗、要点抽取、标准字段整理与报告输出能力。',
    status: '规划中',
    action: '查看模块',
    tone: 'orange',
  },
  {
    code: '03',
    label: '6S标准化报告',
    to: '/6s-report',
    description: '预留 6S 检查记录、问题归集、整改跟踪与标准化报告生成能力。',
    status: '规划中',
    action: '查看模块',
    tone: 'green',
  },
]

const metrics = [
  { key: 'service_request_count', label: '服务需求', index: '01', note: '当前周期累计记录' },
  { key: 'completed_count', label: '已完成', index: '02', note: '已完成处理的需求' },
  { key: 'average_satisfaction', label: '平均满意度', index: '03', note: '统一保留两位小数' },
  { key: 'pending_import_count', label: '待确认批次', index: '04', note: '等待人工确认的数据' },
] as const

async function loadWorkspace() {
  loading.value = true
  apiError.value = ''
  try {
    summary.value = await fetchDashboardSummary()
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
  <section class="dashboard-content">
    <div class="intro-row">
      <div>
        <h2>工作概况</h2>
      </div>
      <div class="date-stamp">
        <span class="date-label">工作区状态</span>
        <strong>{{ loading ? '同步中' : '已就绪' }}</strong>
        <span class="date-line"></span>
      </div>
    </div>

    <div v-if="apiError" class="notice-bar warning"><span class="notice-mark">!</span>{{ apiError }}</div>

    <div class="metrics-grid">
      <article
        v-for="(metric, i) in metrics"
        :key="metric.key"
        class="metric-card"
        :class="{ 'metric-primary': i === 0, 'metric-accent': i === 2 }"
      >
        <div class="metric-topline"><span>{{ metric.label }}</span><span class="metric-index">{{ metric.index }}</span></div>
        <strong>{{ summary[metric.key] }}</strong>
        <p>{{ metric.note }}</p>
      </article>
    </div>

    <section class="module-panel">
      <div class="panel-heading">
        <div>
          <p class="section-kicker">WORKSPACE MODULES</p>
          <h3>部门工作模块</h3>
        </div>
        <span class="module-count">3 个模块</span>
      </div>
      <div class="module-grid">
        <button
          v-for="module in modules"
          :key="module.code"
          class="module-card"
          :class="[`tone-${module.tone}`, { planned: module.status === '规划中' }]"
          type="button"
          @click="router.push(module.to)"
        >
          <span class="module-icon" :class="`tone-${module.tone}`" aria-hidden="true">
            <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
              <template v-if="module.code === '01'">
                <path d="M4 19V5" /><path d="M4 19h16" /><rect x="7" y="11" width="3" height="5" /><rect x="12" y="8" width="3" height="8" /><rect x="17" y="13" width="3" height="3" />
              </template>
              <template v-else-if="module.code === '02'">
                <rect x="9" y="3" width="6" height="11" rx="3" /><path d="M5 11a7 7 0 0 0 14 0" /><path d="M12 18v3" />
              </template>
              <template v-else>
                <path d="M12 3l7 3v6c0 4-3 7-7 9-4-2-7-5-7-9V6z" /><path d="M9 12l2 2 4-4" />
              </template>
            </svg>
          </span>
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
            <p class="section-kicker">ANALYSIS REPORTS</p>
            <h3>分析报表</h3>
          </div>
          <button class="text-button" type="button" @click="router.push('/data-analysis')">查看全部 <span>↗</span></button>
        </div>
        <div v-if="summary.recent_tasks.length === 0" class="empty-state">
          <div class="empty-mark">—</div>
          <strong>还没有分析报表</strong>
          <p>上传第一份 Excel 并完成汇总后，生成的报表会显示在这里。</p>
          <button class="primary-button" type="button" @click="router.push('/data-analysis')">开始导入 <span>↗</span></button>
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
</template>
