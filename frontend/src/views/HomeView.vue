<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { fetchDashboardSummary, type DashboardSummary } from '../api/dashboard'

const router = useRouter()

const loading = ref(true)
const apiError = ref('')
const summary = ref<DashboardSummary>({
  dataset_count: 0,
  sheet_count: 0,
  row_count: 0,
  field_count: 0,
  result_count: 0,
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
  { key: 'dataset_count', label: '数据表', note: '已导入并保留的数据源' },
  { key: 'row_count', label: '数据总行数', note: '全部工作表累计' },
  { key: 'field_count', label: '字段', note: '字段字典去重计数' },
  { key: 'result_count', label: '汇总结果', note: '保留最近 20 份' },
] as const

function formatMetric(key: string, value: number): string {
  return key === 'row_count' ? value.toLocaleString('en-US') : String(value)
}

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
      <h2>工作概况</h2>
      <span class="status-pill"><span class="status-dot"></span>{{ loading ? '同步中' : '工作区已就绪' }}</span>
    </div>

    <div v-if="apiError" class="notice-bar warning"><span class="notice-mark">!</span>{{ apiError }}</div>

    <div class="metrics-grid">
      <article
        v-for="(metric, i) in metrics"
        :key="metric.key"
        class="metric-card"
        :class="{ 'metric-primary': i === 0 }"
      >
        <div class="metric-topline"><span>{{ metric.label }}</span></div>
        <strong>{{ formatMetric(metric.key, summary[metric.key]) }}</strong>
        <p>{{ metric.note }}</p>
      </article>
    </div>

    <section class="module-panel">
      <div class="panel-heading">
        <h3>部门工作模块</h3>
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

  </section>
</template>
