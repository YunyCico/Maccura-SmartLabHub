<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
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
  {
    code: '04',
    label: '服务报告标准化',
    to: '/service-report',
    description: '预留服务报告模板套版、字段标准化与一键生成标准排版报告能力。',
    status: '规划中',
    action: '查看模块',
    tone: 'purple',
  },
]

const metrics = [
  { key: 'dataset_count', label: '数据表', note: '已导入并保留的数据源' },
  { key: 'row_count', label: '数据总行数', note: '全部工作表累计' },
  { key: 'field_count', label: '字段', note: '字段字典去重计数' },
  { key: 'result_count', label: '汇总结果', note: '保留最近 20 份' },
] as const

type TodoSource = '钉钉' | 'Salesforce' | '公司 OA'
type TodoTone = 'blue' | 'orange' | 'green'

type TodoItem = {
  id: number
  source: TodoSource
  tone: TodoTone
  title: string
  detail: string
  due: string
  priority: '高' | '中' | '低'
  completed: boolean
}

// 后续接入真实消息时，只需替换这个数据源，页面结构无需调整。
const todos = ref<TodoItem[]>([
  { id: 1, source: '钉钉', tone: 'blue', title: '确认本周实验室服务问题汇总', detail: '实验室运营管理部 · 待你处理', due: '今天 15:00 前', priority: '高', completed: false },
  { id: 2, source: 'Salesforce', tone: 'orange', title: '跟进重点客户服务反馈', detail: '客户服务记录 · 3 条待跟进', due: '今天', priority: '中', completed: false },
  { id: 3, source: '公司 OA', tone: 'green', title: '审批智慧实验室月度报告', detail: '流程审批 · 发起人：实验室运营管理部', due: '今天 18:00 前', priority: '高', completed: false },
  { id: 4, source: '钉钉', tone: 'blue', title: '查看区域负责人提交的反馈', detail: '群聊消息 · 智慧实验室项目组', due: '明天', priority: '低', completed: false },
])

const pendingTodos = computed(() => todos.value.filter((todo) => !todo.completed))
const completedTodoCount = computed(() => todos.value.length - pendingTodos.value.length)
const orderedTodos = computed(() => [
  ...todos.value.filter((todo) => !todo.completed),
  ...todos.value.filter((todo) => todo.completed),
])

function toggleTodo(todo: TodoItem) {
  todo.completed = !todo.completed
}

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

    <section class="todo-panel" aria-labelledby="today-todos-title">
      <div class="todo-heading">
        <div>
          <p class="section-kicker">WORK QUEUE · TODAY</p>
          <h3 id="today-todos-title">今日待办</h3>
          <p class="todo-subtitle">来自钉钉、Salesforce 和公司 OA 的工作消息，集中在这里处理。</p>
        </div>
        <div class="todo-summary">
          <strong>{{ pendingTodos.length }}</strong>
          <span>项待处理<span v-if="completedTodoCount"> · 已完成 {{ completedTodoCount }} 项</span></span>
        </div>
      </div>

      <div v-if="orderedTodos.length" class="todo-list">
        <article v-for="todo in orderedTodos" :key="todo.id" class="todo-item" :class="{ completed: todo.completed }">
          <button
            class="todo-check"
            :class="{ checked: todo.completed }"
            type="button"
            :aria-label="todo.completed ? `取消完成：${todo.title}` : `完成待办：${todo.title}`"
            @click="toggleTodo(todo)"
          >
            <span aria-hidden="true"></span>
          </button>
          <span class="todo-source" :class="`todo-source-${todo.tone}`">{{ todo.source }}</span>
          <div class="todo-copy">
            <strong>{{ todo.title }}</strong>
            <span>{{ todo.detail }}</span>
          </div>
          <span class="todo-due">{{ todo.due }}</span>
          <span class="todo-priority" :class="`priority-${todo.priority === '高' ? 'high' : todo.priority === '中' ? 'medium' : 'low'}`">{{ todo.priority }}优先级</span>
        </article>
      </div>
      <div v-else class="todo-empty">今天暂无待办事项。</div>
    </section>

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
              <template v-else-if="module.code === '03'">
                <path d="M12 3l7 3v6c0 4-3 7-7 9-4-2-7-5-7-9V6z" /><path d="M9 12l2 2 4-4" />
              </template>
              <template v-else>
                <path d="M7 3h8l4 4v14H7z" /><path d="M15 3v4h4" /><path d="M10 12h6M10 16h6" />
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

    <section class="module-panel">
      <div class="panel-heading">
        <h3>分析报表</h3>
      </div>
      <div class="regions-grid">
        <section
          v-for="m in modules"
          :key="'r-' + m.code"
          class="region-card"
          :class="{ live: m.status === '可用' }"
        >
          <div class="region-head">
            <div class="region-title">
              <h3>{{ m.label }}</h3>
              <p>{{ m.status === '可用' ? '实时统计 · 数据随导入与汇总自动更新' : '报表区域已预留' }}</p>
            </div>
            <button class="region-enter" type="button" @click="router.push(m.to)">进入 ↗</button>
          </div>

          <div v-if="m.status === '可用'" class="region-body">
            <div class="metrics-grid">
              <article
                v-for="metric in metrics"
                :key="metric.key"
                class="metric-card"
                :class="{ 'metric-primary': metric.key === 'dataset_count' }"
              >
                <div class="metric-topline"><span>{{ metric.label }}</span></div>
                <strong>{{ formatMetric(metric.key, summary[metric.key]) }}</strong>
                <p>{{ metric.note }}</p>
              </article>
            </div>
          </div>
          <div v-else class="region-tip">
            该模块功能规划中，开发完成后将在此展示对应的统计数据与报表。
          </div>
        </section>
      </div>
    </section>

  </section>
</template>
