<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { fetchDashboardSummary, type DashboardSummary } from '../api/dashboard'
import { fetchMessages, type MessageFeed, type MessageItem, type MessageSourceKey } from '../api/messages'
import { api } from '../api/http'
import { isAdmin } from '../api/auth'
import { resolveDingTalkAuthCode } from '../lib/dingtalk'
import { loginSalesforce, disconnectSalesforce, fetchSfTodayTasks, fetchSfMyEvents, fetchSfApprovals, fetchSfWeeklyPlans, fetchOaTodos, disconnectOa, fetchDingtalkReminders, type SfSectionResponse, type OaTodoItem, type DingtalkReminder } from '../api/integrations'

const router = useRouter()

const isAdminUser = computed(() => isAdmin())

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

// 待办数据来自后端 /api/messages（钉钉 / Salesforce / OA 聚合）。
// 三家都未配置时后端返回示例数据，此处用 usingPlaceholder 标注出来。
const messageFeed = ref<MessageFeed>({ items: [], sources: [], using_placeholder: false })
const messageLoading = ref(true)
const messageError = ref('')
// 消息接口失败后的自动重试：平台重启（后端短暂不可用）是常态，
// 一次失败就挂一个死错误条会让人以为功能坏了，所以要自己恢复。
const messageRetryTimer = ref<number | null>(null)
const messageRetryCount = ref(0)
const completedIds = ref<Set<string>>(new Set())

// ---------- 自动刷新 ----------
// 目标：不手动点也能及时看到新消息，且不把 Salesforce/钉钉 API 配额打爆。
// 关键约束（实测）：
//   - /api/messages 单次约 2.4~3 秒，间隔太短会拖慢页面
//   - 浏览器会把后台标签页的定时器节流到分钟级，且会冻结页面
// 所以：默认 60 秒；页面不可见时暂停；切回前台立刻补一次。
const AUTO_REFRESH_OPTIONS = [
  { value: 0, label: '关闭' },
  { value: 30, label: '30 秒' },
  { value: 60, label: '60 秒' },
  { value: 300, label: '5 分钟' },
] as const

const AUTO_REFRESH_KEY = 'home_auto_refresh_seconds'
const autoRefreshSeconds = ref<number>(
  Number(localStorage.getItem(AUTO_REFRESH_KEY) ?? 60) || 0,
)
const lastSyncedAt = ref('')
const newItemsCount = ref(0)
let autoRefreshTimer: number | undefined
let lastSeenIds = new Set<string>()

// 上次更新时间的本地显示
const lastSyncedText = computed(() => {
  if (!lastSyncedAt.value) return '尚未同步'
  const d = new Date(lastSyncedAt.value)
  if (Number.isNaN(d.getTime())) return '尚未同步'
  return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
})

// 距下次自动刷新还有多久（秒）
const nextRefreshIn = ref(0)
let countdownTimer: number | undefined

/** 取消待执行的消息接口重试。 */
function clearMessageRetry() {
  if (messageRetryTimer.value !== null) {
    window.clearTimeout(messageRetryTimer.value)
    messageRetryTimer.value = null
  }
}

/** 退避重试：3s → 6s → 12s → 24s → 30s（封顶）。成功后自动停。 */
function scheduleMessageRetry() {
  clearMessageRetry()
  const delay = Math.min(3000 * 2 ** messageRetryCount.value, 30000)
  messageRetryCount.value += 1
  messageRetryTimer.value = window.setTimeout(() => {
    messageRetryTimer.value = null
    // 两个都重试：仪表盘和消息中心会同时因为后端重启而失败
    void loadMessages({ silent: true })
    void loadWorkspace({ silent: true })
  }, delay)
}

function stopAutoRefresh() {
  clearMessageRetry()
  messageRetryCount.value = 0
  if (autoRefreshTimer !== undefined) {
    window.clearInterval(autoRefreshTimer)
    autoRefreshTimer = undefined
  }
  if (countdownTimer !== undefined) {
    window.clearInterval(countdownTimer)
    countdownTimer = undefined
  }
  nextRefreshIn.value = 0
}

function startAutoRefresh() {
  stopAutoRefresh()
  if (!autoRefreshSeconds.value) return
  autoRefreshTimer = window.setInterval(() => {
    // 页面不可见时跳过：浏览器会节流后台定时器，且此时用户也看不到
    if (document.hidden) return
    void loadMessages({ silent: true })
    void loadSalesforceSections()
    void loadOaTodos()
    void loadDingtalkReminders()
  }, autoRefreshSeconds.value * 1000)
  nextRefreshIn.value = autoRefreshSeconds.value
  countdownTimer = window.setInterval(() => {
    if (nextRefreshIn.value > 0) nextRefreshIn.value -= 1
  }, 1000)
}

function setAutoRefresh(seconds: number) {
  autoRefreshSeconds.value = seconds
  localStorage.setItem(AUTO_REFRESH_KEY, String(seconds))
  startAutoRefresh()
}

// 切回前台时立即补一次刷新（后台期间的数据变化在切回时一次性拉回）
function handleVisibilityChange() {
  if (!document.hidden && autoRefreshSeconds.value) {
    void loadMessages({ silent: true })
    void loadSalesforceSections()
    void loadOaTodos()
    void loadDingtalkReminders()
  }
}

const todos = computed<MessageItem[]>(() => messageFeed.value.items)
const usingPlaceholder = computed(() => messageFeed.value.using_placeholder)

const pendingTodos = computed(() => todos.value.filter((todo) => !isCompleted(todo)))
const completedTodoCount = computed(() => todos.value.length - pendingTodos.value.length)

const sourceState = computed(() => {
  const map = {} as Record<MessageSourceKey, { enabled: boolean; count: number; reason: string }>
  for (const item of messageFeed.value.sources) {
    map[item.source] = {
      enabled: item.enabled,
      count: item.count,
      reason: item.error || item.reason || '',
    }
  }
  return map
})

const sourceChips = computed(() =>
  messageFeed.value.sources.map((item) => ({
    key: item.source,
    label: item.source === 'dingtalk' ? '钉钉' : item.source === 'oa' ? '公司 OA' : 'Salesforce',
    tone: item.source === 'dingtalk' ? 'blue' : item.source === 'oa' ? 'green' : 'orange',
    enabled: item.enabled,
    count: item.count,
    reason: item.reason,
    // OA / Salesforce 在消息中心只做连通性探测、不取明细，条数恒为 0，别显示数字
    showCount: item.source === 'dingtalk',
  })),
)

// 消息中心现在只承载钉钉。Salesforce 与 OA 各自有独立的「工作区」板块，
// 明细与连接面板都在那里，消息中心不再重复承载。
const SECTION_LABELS: Record<MessageSourceKey, string> = {
  dingtalk: 'Ding DIng',
  oa: 'OA',
  salesforce: 'Salesforce',
}

const SECTION_ORDER: MessageSourceKey[] = ['dingtalk']

// 钉钉分区内部的功能板块（与设置页「拉取的消息类型」一一对应）。
// 顺序固定，enabled 决定该板块是否在首页出现（设置页关掉了就不显示）。
// hint 是真实接口路径——之前写的是我照记忆编的，全都不合法ApiName。
// 只列首页真的拿得到数据的类型。
// 工作通知 / DING 消息钉钉未开放收件箱查询接口（恒为空板块），
// 审批 / 考勤权限未开通，四类都不在首页占位。
const DINGTALK_KINDS: { key: string; label: string; hint: string }[] = [
  { key: 'todo', label: '待办任务', hint: 'v1.0/todo/users/{unionId}/org/tasks/query' },
  { key: 'calendar', label: '个人日程', hint: 'v1.0/calendar/users/{unionId}/calendars/primary/events' },
  { key: 'trip', label: '部门行程', hint: 'v1.0/calendar/users/{unionId}/calendars/{calendarId}/events' },
]

// 每个分区：标题 + 该来源的待办（未完成在前）+ 状态说明
const notificationSections = computed(() =>
  SECTION_ORDER.map((source) => {
    const chip = sourceChips.value.find((item) => item.key === source)
    const all = todos.value.filter((todo) => todo.source === source)
    const pending = all.filter((todo) => !isCompleted(todo))
    const done = all.filter((todo) => isCompleted(todo))
    return {
      key: source,
      title: SECTION_LABELS[source],
      tone: chip?.tone ?? 'blue',
      enabled: chip?.enabled ?? false,
      reason: chip?.reason ?? '',
      pendingCount: pending.length,
      doneCount: done.length,
      items: [...pending, ...done],
    }
  }),
)

// 钉钉分区：内部按 kind 把待办分到四个功能板块里。
// 后端没返回 kinds（老接口）时退化为显示全部已知类型，不让板块凭空消失。
// kind_notes 是后端给的逐类型说明（未开放接口 / 缺权限 / 调用失败），
// 优先显示它，因为它比静态 hint 更贴近当下的真实状态。
// 被设置页关掉的类型（enabled=false）直接不渲染，而不是显示成灰色空板块。
const dingtalkKinds = computed(() => {
  const state = messageFeed.value.sources.find((item) => item.source === 'dingtalk')
  const enabled = state?.kinds?.length ? state.kinds : DINGTALK_KINDS.map((k) => k.key)
  const notes = state?.kind_notes || {}
  const dd = todos.value.filter((todo) => todo.source === 'dingtalk')
  return DINGTALK_KINDS.filter((kind) => enabled.includes(kind.key)).map((kind) => {
    // 「个人日程」直接用带消息人/头像的提醒数据渲染。
    // 它和「消息提醒」本是同一份日程数据，之前拆成两个板块 →
    // 一个显示 0 条、一个显示 2 条，且提醒浮在网格外不属于任何分区。
    if (kind.key === 'calendar') {
      return {
        ...kind,
        // hint 固定显示这个，权限/错误说明放 note（黄条）里，
        // 两者分开才不会把同一句话显示两遍
        hint: '日程提醒 · 含消息人与头像',
        count: ddReminders.value.length,
        pendingCount: ddReminders.value.length,
        note: ddRemindersNote.value || notes[kind.key] || '',
        items: [],
        rich: true,
      }
    }
    const items = dd.filter((todo) => (todo.kind || 'todo') === kind.key)
    return {
      ...kind,
      count: items.length,
      pendingCount: items.filter((todo) => !isCompleted(todo)).length,
      note: notes[kind.key] || '',
      items: [
        ...items.filter((todo) => !isCompleted(todo)),
        ...items.filter((todo) => isCompleted(todo)),
      ],
      rich: false,
    }
  })
})

// 钉钉分区单独渲染（内部有四个子板块）
const dingtalkSection = computed(
  () => notificationSections.value.find((section) => section.key === 'dingtalk'),
)

// 还没配好凭据的来源。这些来源当前展示的是示例数据，
// 提示里要指名道姓，不能笼统说"三家都未配置"——配好一家后另外两家仍然是示例。
// 消息中心现在只承载钉钉，所以只统计钉钉的未配置情况；
// OA / Salesforce 的连接状态在各自工作区里提示。
const unconfiguredSources = computed(() =>
  sourceChips.value.filter((chip) => !chip.enabled && chip.key === 'dingtalk').map((chip) => chip.label),
)

// Salesforce 首页快速登录：未连接时直接在首页填账号密码 + 腾讯身份验证器 6 位动态码（令牌），
// 后端走「密码 → 动态口令 → 会话」抓 sid 并存回本机配置，之后首页直接拉数据。
// 实例地址用已保存的默认值（maccuracn.my.sfcrmproducts.cn），这里不重复填。
const sfForm = reactive({ username: '', password: '', totp_secret: '' })
const sfLogging = ref(false)
const sfLoginMessage = ref('')
const sfLoginError = ref('')
// 记住账号密码：仅存浏览器本地（localStorage），且只记 用户名 + 密码；
// 令牌（TOTP 密钥）绝不写入，每次登录都必须手动输入。
const SF_REMEMBER_KEY = 'sf_login_remember'
const sfRemember = ref(false)
const salesforceConnected = computed(() => sourceState.value.salesforce?.enabled ?? false)

// 「Salesforce」里的连接面板用这两个值判断收起/展开：
// enabled 只是"凭据还在"，会话是否还有效要看 reason（探活失败会写进来）。
const sfWbConnected = computed(() => sourceState.value.salesforce?.enabled ?? false)
const sfWbReason = computed(() => sourceState.value.salesforce?.reason ?? '')

// 「OA」：连接状态 + 待办列表
const oaConnected = computed(() => sourceState.value.oa?.enabled ?? false)
const oaReason = computed(() => sourceState.value.oa?.reason ?? '')
const oaTodos = ref<OaTodoItem[]>([])
const oaLoading = ref(false)
const oaDisconnecting = ref(false)

// ---------- 钉钉消息提醒（带消息人与头像） ----------
// 钉钉没有收件箱查询接口，能拿到"提醒"的只有日程的 reminders 字段；
// 消息人来自 organizer / attendees，头像需 Contact.User.Read 权限，
// 未开通时后端返回空 avatar_url，这里用姓名首字兜底。

// 钉钉免登拿到的 userid 缓存在本地：requestAuthCode 每次都会弹授权，
// 自动轮询时若每次都去取，用户会被授权弹窗刷屏。
const DD_USER_ID_KEY = 'dd_user_id'
const ddReminders = ref<DingtalkReminder[]>([])
const ddRemindersLoading = ref(false)
const ddRemindersNote = ref('')

// 头像兜底配色：按姓名做稳定散列，同一个人每次颜色一致。
// 用一组中低饱和的底色，白字在上面可读。
const AVATAR_COLORS = ['#1b5faa', '#a45d00', '#28764d', '#7c3aed', '#b03060', '#0f766e', '#b45309', '#3f4c6b']

function avatarColor(name: string): string {
  let hash = 0
  for (let i = 0; i < name.length; i += 1) {
    hash = (hash * 31 + name.charCodeAt(i)) % 100000
  }
  return AVATAR_COLORS[hash % AVATAR_COLORS.length]
}

// 复姓名单：这些姓氏有两个字，取姓氏时不能只取第一个字
const COMPOUND_SURNAMES = new Set([
  '欧阳', '司马', '上官', '诸葛', '夏侯', '东方', '皇甫', '尉迟', '公孙',
  '慕容', '司徒', '司空', '宇文', '长孙', '令狐', '端木', '鲜于', '闾丘',
  '轩辕', '第五', '万俟', '南宫', '呼延', '澹台', '完颜', '赫连',
])

// 取姓氏用于展示：中文习惯用姓氏称呼（"邱"而不是"邱宇"），
// 与头像里显示的那个字保持一致（后端「创建人」用的是同一套规则）。
function surname(name: string): string {
  const text = (name || '').trim()
  if (!text) return ''
  if (text.length >= 2 && COMPOUND_SURNAMES.has(text.slice(0, 2))) return text.slice(0, 2)
  return text.slice(0, 1)
}

// 部门行程的创建人。后端已经识别好（谁的主日历里有这条行程的副本，谁就是创建人），
// 这里只负责渲染成头像。
function tripCreator(item: MessageItem): string {
  const extra = item.extra || {}
  return String(extra.trip_creator || item.creator || '')
}

// 创建时间。钉钉事件详情页"动态"里那条"XX 新建了日程"的时间戳，
// 与接口的 createTime 完全一致（实测分秒不差），显示出来能和钉钉对账。
function createdAtLabel(iso: string): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getMonth() + 1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

async function loadDingtalkReminders() {
  ddRemindersLoading.value = true
  try {
    const userId = localStorage.getItem(DD_USER_ID_KEY) || ''
    const res = await fetchDingtalkReminders(userId)
    ddReminders.value = res.items || []
    ddRemindersNote.value = res.note || ''
  } catch (error) {
    ddReminders.value = []
    console.error(error)
  } finally {
    ddRemindersLoading.value = false
  }
}

// 提醒提前量的文案。全天日程钉钉会返回负数（如 -540），
// 直接显示"开始前 -540 分钟"是错的，这种情况只说"已设提醒"。
function remindLeadLabel(item: DingtalkReminder): string {
  const raw = (item.remind_minutes || '').trim()
  if (!raw) return ''
  const minutes = Number(raw)
  if (!Number.isFinite(minutes) || minutes <= 0) return '已设提醒'
  if (minutes % 60 === 0) return `开始前 ${minutes / 60} 小时提醒`
  return `开始前 ${minutes} 分钟提醒`
}

// 提醒时间的可读文案：今天/明天/具体日期 + 时刻
function remindTimeLabel(item: DingtalkReminder): string {
  const raw = item.start || ''
  if (!raw) return '—'
  const d = new Date(raw)
  if (Number.isNaN(d.getTime())) return raw.slice(0, 16).replace('T', ' ')
  const today = new Date()
  const days = Math.round(
    (new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime() -
      new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime()) /
      86400000,
  )
  if (item.all_day) {
    if (days === 0) return '今天 全天'
    if (days === 1) return '明天 全天'
    if (days === -1) return '昨天 全天'
    return `${d.getMonth() + 1}-${d.getDate()} 全天`
  }
  const clock = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  if (days === 0) return `今天 ${clock}`
  if (days === 1) return `明天 ${clock}`
  if (days === -1) return `昨天 ${clock}`
  return `${d.getMonth() + 1}-${d.getDate()} ${clock}`
}

async function loadOaTodos() {
  oaLoading.value = true
  try {
    const res = await fetchOaTodos()
    oaTodos.value = res.items || []
  } catch (error) {
    oaTodos.value = []
    console.error(error)
  } finally {
    oaLoading.value = false
  }
}

// 断开 OA：后端清掉整段 OA 配置，oaConnected 变 false → 面板自动展开配置引导
async function handleOaDisconnect() {
  if (oaDisconnecting.value) return
  oaDisconnecting.value = true
  try {
    await disconnectOa()
    oaTodos.value = []
    await loadMessages()
    await loadOaTodos()
  } catch (error) {
    console.error(error)
  } finally {
    oaDisconnecting.value = false
  }
}

// Salesforce（要批准的项目 / 今天的任务 / 销售周计划）。
// 三个独立板块，走 /api/integrations/salesforce/* 专用接口，不走聚合消息流。
type SfBucket = {
  items: Record<string, any>[]
  loading: boolean
  connected: boolean
  error: string
}
const sfSections = reactive<{ tasks: SfBucket; events: SfBucket; approvals: SfBucket; plans: SfBucket }>({
  tasks: { items: [], loading: false, connected: false, error: '' },
  events: { items: [], loading: false, connected: false, error: '' },
  approvals: { items: [], loading: false, connected: false, error: '' },
  plans: { items: [], loading: false, connected: false, error: '' },
})
const sfInstanceUrl = ref('')

async function loadSalesforceSections() {
  const jobs: [keyof typeof sfSections, () => Promise<SfSectionResponse>][] = [
    ['tasks', fetchSfTodayTasks],
    ['events', fetchSfMyEvents],
    ['approvals', fetchSfApprovals],
    ['plans', fetchSfWeeklyPlans],
  ]
  await Promise.all(
    jobs.map(async ([key, fn]) => {
      const bucket = sfSections[key]
      bucket.loading = true
      try {
        const res = await fn()
        bucket.items = res.items || []
        bucket.connected = res.connected
        bucket.error = res.error || ''
        if (res.instance_url) sfInstanceUrl.value = res.instance_url
      } catch (error) {
        bucket.connected = false
        bucket.error = 'Salesforce 板块加载失败，请确认后端已启动。'
        console.error(error)
      } finally {
        bucket.loading = false
      }
    }),
  )
}

function openUrl(url: string) {
  if (!url) return
  // Salesforce 记录链接是绝对地址：转成站内相对路径后走免登跳转，
  // 否则浏览器没有会话，会被 Salesforce 踢回登录页。
  const path = toSfPath(url)
  if (path) {
    openSalesforce(path)
    return
  }
  window.open(url, '_blank', 'noopener')
}

/**
 * 把 Salesforce 绝对地址转成站内相对路径；不是 Salesforce 的地址就返回空。
 *
 * 主机判断不只认配置里的 instance_url —— 首页数据还没加载完时它可能是空的，
 * 这时靠域名后缀（sfcrmproducts.cn / sfcrmapps.cn）照样能认出 Salesforce 链接。
 */
function toSfPath(url: string): string {
  if (!url) return ''
  try {
    const parsed = new URL(url, window.location.origin)
    const host = parsed.hostname.toLowerCase()
    const base = (sfInstanceUrl.value || '').replace(/\/$/, '')
    const isSf = (base && parsed.origin === new URL(base).origin)
      || /\.sfcrmproducts\.cn$/.test(host)
      || /\.sfcrmapps\.cn$/.test(host)
    if (!isSf) return ''
    return `${parsed.pathname}${parsed.search}`
  } catch {
    return ''
  }
}

/**
 * 免登打开 Salesforce 原生页。
 *
 * 流程全部在后端完成：浏览器访问
 * `GET /api/integrations/salesforce/launch?path=...` → 后端用 sid 换
 * Salesforce 官方的 frontdoor URL → 302 → 浏览器自动认领会话，不用再登。
 *
 * 为什么是「一次同步导航」而不是「先开空白页、再异步取令牌」：
 * 后者一旦中间任何一步失败（后端没起、会话过期、接口 404），
 * 就会在浏览器里留下一个 about:blank 的空标签页，而错误信息
 * 只在平台页里，用户只看到一片空白。现在失败时后端直接返回
 * 一页可读的说明，新标签页里就能看到原因。
 *
 * `window.open` 必须在点击的**同步栈**里调用，否则会被弹窗拦截器挡掉；
 * 真被挡时退化为当前页跳转，保证用户一定能到 Salesforce。
 */
function openSalesforce(path: string) {
  const url = `/api/integrations/salesforce/launch?path=${encodeURIComponent(path)}`
  // ⚠️ 这里**不能**用 `window.open(url, '_blank', 'noopener')`：
  // 带 noopener 时 window.open 一律返回 null，「弹窗是否被拦」的判断就永远成立，
  // 结果当前页也跟着跳走 —— 会开两个标签页。
  // 正确做法：先同步开一个空白页拿到 WindowProxy，立刻断开 opener，再同步赋地址。
  // 全程没有 await，中间不会停在空白页。
  const win = window.open('', '_blank')
  if (win) {
    try { win.opener = null } catch { /* 忽略：断掉反向 tabnabbing，替代 noopener */ }
    win.location.href = url
  } else {
    // 弹窗被拦：当前页直接跳过去（浏览器的后退键还能回到平台）
    window.location.href = url
  }
}

function formatDate(iso: string): string {
  if (!iso) return ''
  // 2026-10-13T03:42:43.000+0000 -> 2026-10-13 03:42
  return iso.replace('T', ' ').slice(0, 16)
}

function planDateRange(start: string, end: string): string {
  if (!start && !end) return ''
  return `${start || '—'} ~ ${end || '—'}`
}

function isToday(dateStr: string): boolean {
  if (!dateStr) return false
  const now = new Date()
  const y = now.getFullYear()
  const m = String(now.getMonth() + 1).padStart(2, '0')
  const d = String(now.getDate()).padStart(2, '0')
  return dateStr === `${y}-${m}-${d}`
}

function taskDueLabel(item: Record<string, any>): string {
  if (isToday(item.activity_date)) return '今天'
  if (item.activity_date) return `逾期 ${item.activity_date}`
  return '无期限'
}

// 日程时间：后端已换算成东八区，取 "YYYY-MM-DDTHH:mm" 的后 5 位即 HH:mm
function eventTimeLabel(iso: string): string {
  if (!iso) return ''
  const m = iso.match(/T(\d{2}:\d{2})/)
  return m ? m[1] : ''
}

function eventDayLabel(iso: string): string {
  if (!iso) return ''
  return iso.slice(0, 10)
}

function eventDueLabel(item: Record<string, any>): string {
  const day = eventDayLabel(item.end)
  if (isToday(day)) return '今天'
  if (day) return day.slice(5)
  return '—'
}

function planStatusClass(status: string): string {
  const s = (status || '').toLowerCase()
  if (s.includes('批准') || s.includes('approv')) return 'is-approved'
  if (s.includes('审批中') || s.includes('待') || s.includes('提交') || s.includes('pending')) return 'is-pending'
  return 'is-neutral'
}

async function handleSalesforceLogin() {
    if (!sfForm.username.trim() || !sfForm.password || !sfForm.totp_secret.trim()) {
      sfLoginError.value = '请填写用户名、密码和腾讯身份验证器的 6 位动态码（令牌）。'
      return
    }
  sfLogging.value = true
  sfLoginMessage.value = ''
  sfLoginError.value = ''
  try {
    const result = await loginSalesforce({
      username: sfForm.username.trim(),
      password: sfForm.password,
      totp_secret: sfForm.totp_secret.trim(),
      remember: sfRemember.value,
    })
    if (result.ok) {
      sfLoginMessage.value = result.detail || '登录成功，已获取会话'
      // 记住账号密码（存浏览器本地）；令牌不记，始终手输
      if (sfRemember.value) {
        localStorage.setItem(
          SF_REMEMBER_KEY,
          JSON.stringify({ username: sfForm.username, password: sfForm.password }),
        )
      } else {
        localStorage.removeItem(SF_REMEMBER_KEY)
      }
      sfForm.password = ''
      sfForm.totp_secret = ''
      await loadMessages()
      await loadSalesforceSections()
    } else {
      sfLoginError.value = result.detail || '登录失败'
    }
  } catch (error) {
    sfLoginError.value = '登录请求失败，请确认后端已启动。'
    console.error(error)
  } finally {
    sfLogging.value = false
  }
}

// 断开 Salesforce：清掉后端存的会话 id，连接面板自动展开回登录界面。
const sfDisconnecting = ref(false)
async function handleSfDisconnect() {
  if (sfDisconnecting.value) return
  sfDisconnecting.value = true
  try {
    const result = await disconnectSalesforce()
    if (result.ok) {
      sfLoginMessage.value = ''
      sfLoginError.value = ''
      // 断开后本地也不该留凭据
      localStorage.removeItem(SF_REMEMBER_KEY)
      sfRemember.value = false
      sfForm.password = ''
      sfForm.totp_secret = ''
      await loadMessages()
      await loadSalesforceSections()
    } else {
      sfLoginError.value = result.detail || '断开失败'
    }
  } catch (error) {
    sfLoginError.value = '断开请求失败，请确认后端已启动。'
    console.error(error)
  } finally {
    sfDisconnecting.value = false
  }
}

// 标题右侧的三个来源配置入口，点击统一跳到消息互通设置页
const configActions = computed(() => {
  const state = sourceState.value
  const build = (key: MessageSourceKey, label: string, tone: string, title: string) => ({
    key,
    label,
    tone,
    title,
    enabled: state[key]?.enabled ?? false,
    count: state[key]?.count ?? 0,
    // Salesforce 在消息中心只做连通性探测、不取明细，条数恒为 0，别显示数字
    showCount: key !== 'salesforce',
  })
  return [
    build('dingtalk', '钉钉', 'blue', '配置钉钉企业内部应用凭据与消息类型'),
    build('oa', '公司 OA', 'green', '配置公司 OA 接口地址、路径与字段映射'),
    build('salesforce', 'Salesforce', 'orange', '登录或配置 Salesforce 接入'),
  ]
})

function todoKey(todo: MessageItem): string {
  return `${todo.source}:${todo.external_id || todo.title}`
}

function isCompleted(todo: MessageItem): boolean {
  return completedIds.value.has(todoKey(todo))
}

function toggleTodo(todo: MessageItem) {
  const key = todoKey(todo)
  const next = new Set(completedIds.value)
  if (next.has(key)) {
    next.delete(key)
  } else {
    next.add(key)
  }
  completedIds.value = next
}

function openTodo(todo: MessageItem) {
  // 走 openUrl：Salesforce 来源的待办需要免登跳转，钉钉/OA 的保持原样
  if (todo.url) openUrl(todo.url)
}

function formatMetric(key: string, value: number): string {
  return key === 'row_count' ? value.toLocaleString('en-US') : String(value)
}

async function loadWorkspace(opts: { silent?: boolean } = {}) {
  // 静默刷新（重试/轮询触发的）不显示全屏 loading，避免页面反复闪骨架屏
  if (!opts.silent) {
    loading.value = true
  }
  try {
    summary.value = await fetchDashboardSummary()
    // 后端恢复了就撤掉提示条（平台重启时会短暂失败，属可自愈）
    apiError.value = ''
  } catch (error) {
    apiError.value = '暂时无法连接后端服务，当前显示本地空状态，正在自动重试…'
    scheduleMessageRetry()
    console.error(error)
  } finally {
    if (!opts.silent) {
      loading.value = false
    }
  }
}

// 钉钉免登拿到的 userid 缓存到本地：requestAuthCode 每次都会弹授权，
// 自动轮询时若每次都去取，用户会被授权弹窗刷屏。
// 只在首次（无缓存）或后端明确说这个 userid 无效时才重新授权。
async function loadMessages(opts: { silent?: boolean } = {}) {
  // 静默刷新（轮询触发的）不显示全屏 loading，避免页面每分钟闪一次
  if (!opts.silent) {
    messageLoading.value = true
  }
  // ⚠️ 这里**不要**先清空 messageError：重试期间错误条应该一直挂着，
  //    等真的成功了再撤（成功时在下面清）。否则会一闪一闪的。
  try {
    let userId = localStorage.getItem(DD_USER_ID_KEY) || ''
    if (!userId) {
      // 在钉钉容器内先免登换 userid，拿不到就空着，后端会回退到配置默认值
      const code = await resolveDingTalkAuthCode(import.meta.env.VITE_DINGTALK_CORP_ID || '')
      if (code) {
        userId = await resolveUserId(code)
        if (userId) localStorage.setItem(DD_USER_ID_KEY, userId)
      }
    }
    const feed = await fetchMessages(userId)
    messageFeed.value = feed
    // 恢复了就撤掉错误条和重试
    messageError.value = ''
    messageRetryCount.value = 0
    clearMessageRetry()
    lastSyncedAt.value = feed.fetched_at || new Date().toISOString()
    // 统计"上次同步之后新出现"的条目，给用户一个显眼的提示
    const ids = new Set((feed.items || []).map((i) => `${i.source}:${i.external_id || i.title}`))
    if (lastSeenIds.size) {
      let added = 0
      ids.forEach((id) => {
        if (!lastSeenIds.has(id)) added += 1
      })
      newItemsCount.value = added
    }
    lastSeenIds = ids
    // 后端说这个 userid 取不到数据时，说明授权过期了，下次重新授权
    const dingtalk = feed.sources?.find((s) => s.source === 'dingtalk')
    if (userId && dingtalk && !dingtalk.enabled) {
      localStorage.removeItem(DD_USER_ID_KEY)
    }
  } catch (error) {
    // 平台重启时后端会短暂不可用，这是可自愈的，别摆一条吓人的死错误条
    messageError.value = '后端服务暂时不可用，正在自动重试…'
    scheduleMessageRetry()
    console.error(error)
  } finally {
    if (!opts.silent) {
      messageLoading.value = false
    }
  }
}

async function resolveUserId(code: string): Promise<string> {
  try {
    const response = await api.get<{ user_id: string }>('/user/me', { params: { code } })
    return response.data.user_id || ''
  } catch {
    return ''
  }
}

onMounted(() => {
  loadWorkspace()
  loadMessages()
  loadSalesforceSections()
  loadOaTodos()
  loadDingtalkReminders()
  // 回填已记住的账号密码（绝不回填令牌）
  try {
    const raw = localStorage.getItem(SF_REMEMBER_KEY)
    if (raw) {
      const saved = JSON.parse(raw) as { username?: string; password?: string }
      if (saved.username) sfForm.username = saved.username
      if (saved.password) sfForm.password = saved.password
      sfRemember.value = true
    }
  } catch {
    // 本地数据损坏则忽略，不影响登录
  }
  startAutoRefresh()
  document.addEventListener('visibilitychange', handleVisibilityChange)
})

onUnmounted(() => {
  stopAutoRefresh()
  document.removeEventListener('visibilitychange', handleVisibilityChange)
})
</script>

<template>
  <section class="dashboard-content">
    <div class="intro-row">
      <h2>工作概况</h2>
      <span class="status-pill"><span class="status-dot"></span>{{ loading ? '同步中' : '工作区已就绪' }}</span>
    </div>

    <div v-if="apiError" class="notice-bar warning"><span class="notice-mark">!</span>{{ apiError }}</div>

    <section class="todo-panel" aria-labelledby="notifications-center-title">
      <div class="todo-heading">
        <div class="todo-heading-main">
          <p class="section-kicker">NOTIFICATIONS CENTER</p>
          <h3 id="notifications-center-title">Notifications Center</h3>
          <p class="todo-subtitle">钉钉的待办与日程集中在这里处理；Salesforce 与 OA 各自在下方区域。</p>
          <div class="todo-config-actions">
            <button
              v-for="action in configActions"
              :key="action.key"
              class="config-action"
              :class="`config-action-${action.tone}`"
              type="button"
              :title="action.title"
              @click="router.push('/settings/integrations')"
            >
              <span class="config-action-dot"></span>{{ action.label }}
              <span v-if="action.enabled && action.showCount" class="config-action-count">{{ action.count }}</span>
            </button>
          </div>
          <div v-if="sourceChips.length" class="todo-sources">
            <span
              v-for="chip in sourceChips"
              :key="chip.key"
              class="todo-source-chip"
              :class="[`chip-${chip.tone}`, { live: chip.enabled }]"
              :title="chip.reason || (chip.enabled ? (chip.showCount ? `已连接 · ${chip.count} 条` : '已连接') : '未配置凭据')"
            >
              <span class="chip-dot"></span>{{ chip.label }}
              <em v-if="chip.enabled && chip.showCount">{{ chip.count }}</em>
            </span>
            <button class="todo-refresh" type="button" :disabled="messageLoading" @click="loadMessages()">
              {{ messageLoading ? '刷新中…' : '刷新' }}
            </button>
            <!-- 自动刷新：间隔可选 + 显示上次更新时间 + 倒计时 -->
            <label class="auto-refresh" title="数据按间隔自动更新；页面切到后台时暂停，切回来立即补一次">
              <span class="auto-refresh-label">自动</span>
              <select
                class="auto-refresh-select"
                :value="autoRefreshSeconds"
                @change="setAutoRefresh(Number(($event.target as HTMLSelectElement).value))"
              >
                <option v-for="opt in AUTO_REFRESH_OPTIONS" :key="opt.value" :value="opt.value">
                  {{ opt.label }}
                </option>
              </select>
            </label>
            <span class="auto-refresh-status">
              <template v-if="autoRefreshSeconds">
                上次更新 {{ lastSyncedText }}<template v-if="nextRefreshIn > 0"> · {{ nextRefreshIn }}s 后刷新</template>
              </template>
              <template v-else>上次更新 {{ lastSyncedText }}（自动刷新已关闭）</template>
            </span>
            <span v-if="newItemsCount > 0" class="auto-refresh-new">{{ newItemsCount }} 条新消息</span>
            <button class="todo-refresh" type="button" @click="router.push('/settings/integrations')">
              消息设置
            </button>
          </div>
        </div>
        <div class="todo-summary">
          <strong>{{ pendingTodos.length }}</strong>
          <span>项待处理<span v-if="completedTodoCount"> · 已完成 {{ completedTodoCount }} 项</span></span>
        </div>
      </div>

      <div v-if="messageError" class="notice-bar warning"><span class="notice-mark">!</span>{{ messageError }}</div>
      <div v-else-if="usingPlaceholder" class="notice-bar info">
        <span class="notice-mark">i</span>
        {{ unconfiguredSources.join('、') || '消息接口' }}尚未配置凭据，该来源当前展示示例数据。到
        <button type="button" class="notice-link" @click="router.push('/settings/integrations')">消息互通设置</button>
        填入后自动切换为真实消息。
      </div>

      <div class="nc-sources">
        <!-- 钉钉：与 Salesforce / OA 同级的第三个来源分区 -->
        <section
          v-if="dingtalkSection"
          class="nc-source nc-source-dingtalk"
        >
          <header class="nc-source-head">
            <span class="nc-source-dot nc-dot-blue"></span>
            <h4 class="nc-source-name">{{ dingtalkSection.title }}</h4>
            <span class="nc-source-count">{{ dingtalkSection.pendingCount }}</span>
            <span
              class="nc-source-state"
              :class="{ 'is-live': dingtalkSection.enabled, 'is-warn': dingtalkSection.enabled && !!dingtalkSection.reason }"
              :title="dingtalkSection.reason || (dingtalkSection.enabled ? '已连接' : '未配置凭据')"
            >{{ dingtalkSection.enabled ? '已连接' : '未配置' }}</span>
          </header>
          <p v-if="dingtalkSection.reason" class="nc-source-reason">{{ dingtalkSection.reason }}</p>

          <div class="nc-kinds">
            <section
              v-for="kind in dingtalkKinds"
              :key="kind.key"
              class="nc-kind"
            >
              <header class="nc-kind-head">
                <span class="nc-kind-dot"></span>
                <h5>{{ kind.label }}</h5>
                <span class="nc-kind-count">{{ kind.pendingCount }}</span>
                <span class="nc-kind-hint">{{ kind.hint }}</span>
              </header>
              <p v-if="kind.note" class="nc-kind-note">{{ kind.note }}</p>

              <!-- 个人日程：用带消息人/头像的提醒数据渲染（与"消息提醒"是同一份数据） -->
              <template v-if="kind.rich">
                <div v-if="ddRemindersLoading" class="remind-empty">加载中…</div>
                <div v-else-if="ddReminders.length" class="remind-list">
                  <article
                    v-for="item in ddReminders"
                    :key="item.id"
                    class="remind-item"
                  >
                    <div class="remind-avatars">
                      <span
                        v-for="person in item.people.slice(0, 3)"
                        :key="`${item.id}-${person.id}-${person.role}`"
                        class="remind-avatar"
                        :class="{ 'is-organizer': person.role === 'organizer' }"
                        :style="{ background: avatarColor(person.name) }"
                        :title="`${person.name}${person.role === 'organizer' ? '（消息人）' : ''}`"
                      >
                        <img v-if="person.avatar_url" :src="person.avatar_url" :alt="person.name" />
                        <template v-else>{{ surname(person.name) }}</template>
                      </span>
                      <span v-if="item.people.length > 3" class="remind-more">+{{ item.people.length - 3 }}</span>
                    </div>
                    <div class="remind-copy">
                      <strong>{{ item.title }}</strong>
                      <span>
                        {{ surname(item.people[0]?.name) || '未知' }} 发起
                        <template v-if="item.people.length > 1"> · {{ item.people.length }} 人</template>
                        <template v-if="remindLeadLabel(item)"> · {{ remindLeadLabel(item) }}</template>
                      </span>
                    </div>
                    <span class="remind-time">{{ remindTimeLabel(item) }}</span>
                  </article>
                </div>
                <div v-else class="todo-empty">本周（周一至周日）没有日程提醒。</div>
              </template>

              <div v-else-if="kind.items.length" class="todo-list">
                <article
                  v-for="todo in kind.items"
                  :key="todoKey(todo)"
                  class="todo-item"
                  :class="{ completed: isCompleted(todo) }"
                  @click="openTodo(todo)"
                >
                  <button
                    class="todo-check"
                    :class="{ checked: isCompleted(todo) }"
                    type="button"
                    :aria-label="isCompleted(todo) ? `取消完成：${todo.title}` : `完成待办：${todo.title}`"
                    @click.stop="toggleTodo(todo)"
                  >
                    <span aria-hidden="true"></span>
                  </button>
                  <div class="todo-copy">
                    <strong>{{ todo.title }}</strong>
                    <span>
                      <!-- 创建人用头像表示，和钉钉日程详情"动态"里的头像一致 -->
                      <span
                        v-if="tripCreator(todo)"
                        class="todo-creator"
                        :title="`创建人 ${tripCreator(todo)}`"
                      >
                        <span class="remind-avatar" :style="{ background: avatarColor(tripCreator(todo)) }">
                          {{ surname(tripCreator(todo)) }}
                        </span>
                        <em>创建人 {{ surname(tripCreator(todo)) }}</em>
                      </span>
                      <!-- 创建时间 = 钉钉"动态"里的时间戳，用来对账 -->
                      <span
                        v-if="todo.extra?.created_at"
                        class="todo-created"
                        :title="`创建于 ${createdAtLabel(todo.extra.created_at)}`"
                      >创建于 {{ createdAtLabel(todo.extra.created_at) }}</span>
                      {{ todo.detail }}
                    </span>
                  </div>
                  <span class="todo-due">{{ todo.due || '无期限' }}</span>
                  <span class="todo-priority" :class="`priority-${todo.priority === '高' ? 'high' : todo.priority === '中' ? 'medium' : 'low'}`">{{ todo.priority }}优先级</span>
                </article>
              </div>
              <div v-else class="todo-empty">
                暂无内容。
              </div>
            </section>
          </div>
          <div v-if="!dingtalkKinds.length" class="todo-empty">
            钉钉消息类型已在消息设置中全部关闭。
          </div>
        </section>

        <!-- Salesforce：与钉钉、OA 同级的第二个来源分区 -->
        <section class="nc-source nc-source-salesforce" aria-labelledby="sf-wb-title">
        <header class="nc-source-head">
          <span class="nc-source-dot nc-dot-orange"></span>
          <h4 class="nc-source-name" id="sf-wb-title">Salesforce</h4>
        </header>
        <p class="nc-source-sub">
          审批、今日任务与周计划集中处理，数据来自{{ salesforceConnected ? '已连接的 Salesforce' : ' Salesforce（未连接时点击卡片内"去连接"）' }}。
        </p>

        <!-- Salesforce 连接面板：未连接时展开登录界面；连接成功后自动收起，
             只留一条「已连接」状态行，鼠标移上去才浮现「断开连接」。
             断开后自动向下展开回登录界面。
             注意：不能只判断 enabled —— 会话过期时 enabled 仍为 true（配置还在），
             若不把 reason 也纳入条件，用户会被"假已连接"挡住，无处重登。 -->
        <div class="sf-panel">
          <div
            class="sf-panel-bar"
            :class="{ collapsed: sfWbConnected && !sfWbReason }"
            @click="sfWbConnected && !sfWbReason ? router.push('/settings/integrations') : undefined"
          >
            <template v-if="sfWbConnected && !sfWbReason">
              <span class="sf-conn-ok"><span class="sf-conn-mark">✓</span>Salesforce 已连接</span>
              <span class="sf-conn-hint">会话有效，下方卡片即为实时数据</span>
              <button
                class="sf-disconnect-btn"
                type="button"
                :disabled="sfDisconnecting"
                @click.stop="handleSfDisconnect"
              >{{ sfDisconnecting ? '断开中…' : '断开连接' }}</button>
            </template>
            <template v-else-if="sfWbConnected">
              <span class="sf-conn-ok is-warn"><span class="sf-conn-mark">!</span>Salesforce 会话已失效</span>
              <span class="sf-conn-hint">请重新登录以恢复取数</span>
            </template>
            <template v-else>
              <span class="sf-conn-ok"><span class="sf-conn-mark">○</span>Salesforce 未连接</span>
              <span class="sf-conn-hint">登录后此处仅提示连接状态，明细见下方卡片</span>
            </template>
          </div>

          <!-- 登录表单：连接成功时折叠收起（CSS 过渡实现滑动效果） -->
          <div class="sf-panel-body" :class="{ collapsed: sfWbConnected && !sfWbReason }">
            <div class="sf-panel-body-inner">
              <div class="sf-quick-login">
                <p class="sf-quick-title">登录 Salesforce 抓取会话</p>
                <p class="sf-quick-sub">
                  填写账号、密码，以及<b>腾讯身份验证器（Tencent Authenticator）</b>实时生成的 6 位动态码（令牌）。登录后系统自动抓取会话 sid 并存储到本机。动态码 30 秒刷新一次，绝不记忆。
                </p>
                <div class="sf-form">
                  <label class="sf-field">
                    <span>用户名</span>
                    <input v-model="sfForm.username" type="text" placeholder="组织内邮箱" autocomplete="username" />
                  </label>
                  <label class="sf-field">
                    <span>密码</span>
                    <input v-model="sfForm.password" type="password" placeholder="Salesforce 密码" autocomplete="current-password" />
                  </label>
                  <label class="sf-field">
                    <span>腾讯身份验证器 6 位动态码（令牌）</span>
                    <input v-model="sfForm.totp_secret" type="text" inputmode="numeric" maxlength="6" placeholder="6 位数字，30 秒内有效" autocomplete="off" />
                  </label>
                </div>
                <label class="sf-remember">
                  <input v-model="sfRemember" type="checkbox" />
                  <span>记住账号密码（令牌不会被记住，每次需手动输入）</span>
                </label>
                <div class="sf-actions">
                  <button class="sf-login-btn" type="button" :disabled="sfLogging" @click="handleSalesforceLogin">
                    {{ sfLogging ? '登录中…' : '登录并抓取会话' }}
                  </button>
                  <button class="sf-settings-btn" type="button" @click="router.push('/settings/integrations')">高级设置</button>
                </div>
                <div v-if="sfLoginMessage" class="test-result ok"><span>✓</span>{{ sfLoginMessage }}</div>
                <div v-if="sfLoginError" class="test-result fail"><span>✕</span>{{ sfLoginError }}</div>
              </div>
            </div>
          </div>
        </div>

        <div class="sf-wb-grid">
          <!-- 要批准的项目 -->
          <article class="sf-card" :class="{ live: sfSections.approvals.connected }">
            <header class="sf-card-head">
              <span class="sf-card-dot"></span>
              <h4>要批准的项目</h4>
              <span class="sf-card-count">{{ sfSections.approvals.items.length }}</span>
            </header>
            <div class="sf-card-body">
              <div v-if="sfSections.approvals.loading" class="sf-card-loading">加载中…</div>
              <ul v-else-if="sfSections.approvals.items.length" class="sf-list">
                <li
                  v-for="it in sfSections.approvals.items"
                  :key="it.id"
                  class="sf-list-item sf-list-item-block"
                  @click="openUrl(it.url)"
                >
                  <div class="sf-list-copy">
                    <div class="sf-approval-head">
                      <strong>{{ it.target_name }}</strong>
                      <span class="sf-approval-doc" v-if="it.doc_type">{{ it.doc_type }}</span>
                    </div>
                    <!-- 按单据类型分发的字段：单据类型 / 终端客户 / 二级-三级分类 / 反馈日期 … -->
                    <dl v-if="(it.fields || []).length" class="sf-approval-fields">
                      <div v-for="f in it.fields" :key="f.label" class="sf-approval-field">
                        <dt>{{ f.label }}</dt>
                        <dd>{{ f.value }}</dd>
                      </div>
                    </dl>
                    <span v-else class="sf-approval-fallback">
                      {{ it.target_type || '记录' }} · 提交于 {{ formatDate(it.created_date) }}
                    </span>
                  </div>
                  <span class="sf-arrow" aria-hidden="true">↗</span>
                </li>
              </ul>
              <div v-else class="sf-card-empty">
                <template v-if="sfSections.approvals.connected">
                  暂无待我审批的项目。
                </template>
                <template v-else>
                  <p>Salesforce 未连接或会话已失效。</p>
                  <button class="sf-link" type="button" @click="router.push('/settings/integrations')">去连接 ↗</button>
                </template>
              </div>
            </div>
            <footer class="sf-card-actions">
              <button class="sf-quick-action" type="button" @click="openSalesforce('/lightning/o/ProcessInstanceWorkitem/list')">去审批 ↗</button>
              <button class="sf-quick-action ghost" type="button" :disabled="sfSections.approvals.loading" @click="loadSalesforceSections">刷新</button>
            </footer>
          </article>

          <!-- 今天的任务（口径与 Salesforce 原生首页「今天的任务」一致：我名下的任务） -->
          <article class="sf-card" :class="{ live: sfSections.tasks.connected }">
            <header class="sf-card-head">
              <span class="sf-card-dot"></span>
              <h4>今天的任务</h4>
              <span class="sf-card-count">{{ sfSections.tasks.items.length }}</span>
            </header>
            <div class="sf-card-body">
              <div v-if="sfSections.tasks.loading" class="sf-card-loading">加载中…</div>
              <ul v-else-if="sfSections.tasks.items.length" class="sf-list">
                <li
                  v-for="it in sfSections.tasks.items"
                  :key="it.id"
                  class="sf-list-item"
                  @click="openUrl(it.url)"
                >
                  <div class="sf-list-copy">
                    <strong>{{ it.subject }}</strong>
                    <span>{{ it.what_name || it.owner_name || 'Salesforce 任务' }} · {{ it.status }} · {{ it.priority }}优先级</span>
                  </div>
                  <span class="sf-due" :class="{ overdue: !isToday(it.activity_date) }">{{ taskDueLabel(it) }}</span>
                </li>
              </ul>
              <div v-else class="sf-card-empty">
                <template v-if="sfSections.tasks.connected">
                  <p>你名下今天没有到期或逾期的任务。</p>
                  <small>与 Salesforce 首页「今天的任务」口径一致，仅显示分配给你的任务。</small>
                </template>
                <template v-else>
                  <p>Salesforce 未连接或会话已失效。</p>
                  <button class="sf-link" type="button" @click="router.push('/settings/integrations')">去连接 ↗</button>
                </template>
              </div>
            </div>
            <footer class="sf-card-actions">
              <button class="sf-quick-action" type="button" @click="openSalesforce('/lightning/o/Task/list')">查看全部 ↗</button>
              <button class="sf-quick-action ghost" type="button" @click="openSalesforce('/lightning/o/Task/new')">新建任务</button>
              <button class="sf-quick-action ghost" type="button" :disabled="sfSections.tasks.loading" @click="loadSalesforceSections">刷新</button>
            </footer>
          </article>

          <!-- 今日日程（对应 Salesforce 原生首页「今日事件」面板） -->
          <article class="sf-card" :class="{ live: sfSections.events.connected }">
            <header class="sf-card-head">
              <span class="sf-card-dot"></span>
              <h4>今日日程</h4>
              <span class="sf-card-count">{{ sfSections.events.items.length }}</span>
            </header>
            <div class="sf-card-body">
              <div v-if="sfSections.events.loading" class="sf-card-loading">加载中…</div>
              <ul v-else-if="sfSections.events.items.length" class="sf-list">
                <li
                  v-for="it in sfSections.events.items"
                  :key="it.id"
                  class="sf-list-item"
                  @click="openUrl(it.url)"
                >
                  <div class="sf-list-copy">
                    <strong>{{ it.subject }}</strong>
                    <span>{{ eventTimeLabel(it.start) }}</span>
                  </div>
                  <span class="sf-due" :class="{ overdue: !isToday(it.end) }">{{ eventDueLabel(it) }}</span>
                </li>
              </ul>
              <div v-else class="sf-card-empty">
                <template v-if="sfSections.events.connected">
                  今天及以后没有安排日程。
                </template>
                <template v-else>
                  <p>Salesforce 未连接或会话已失效。</p>
                  <button class="sf-link" type="button" @click="router.push('/settings/integrations')">去连接 ↗</button>
                </template>
              </div>
            </div>
            <footer class="sf-card-actions">
              <button class="sf-quick-action" type="button" @click="openSalesforce('/lightning/o/Event/list')">查看全部 ↗</button>
              <button class="sf-quick-action ghost" type="button" @click="openSalesforce('/lightning/o/Event/new')">新建日程</button>
              <button class="sf-quick-action ghost" type="button" :disabled="sfSections.events.loading" @click="loadSalesforceSections">刷新</button>
            </footer>
          </article>

          <!-- 销售周计划 -->
          <article class="sf-card" :class="{ live: sfSections.plans.connected }">
            <header class="sf-card-head">
              <span class="sf-card-dot"></span>
              <h4>销售周计划</h4>
              <span class="sf-card-count">{{ sfSections.plans.items.length }}</span>
            </header>
            <div class="sf-card-body">
              <div v-if="sfSections.plans.loading" class="sf-card-loading">加载中…</div>
              <ul v-else-if="sfSections.plans.items.length" class="sf-list">
                <li
                  v-for="it in sfSections.plans.items"
                  :key="it.id"
                  class="sf-list-item"
                  @click="openUrl(it.url)"
                >
                  <div class="sf-list-copy">
                    <strong>{{ it.week_name || it.name }}</strong>
                    <span>{{ it.company || '未填公司' }} · {{ planDateRange(it.start_date, it.end_date) }}</span>
                  </div>
                  <span class="sf-status" :class="planStatusClass(it.status)">{{ it.status || '—' }}</span>
                </li>
              </ul>
              <div v-else class="sf-card-empty">
                <template v-if="sfSections.plans.connected">
                  暂无销售周计划。
                </template>
                <template v-else>
                  <p>Salesforce 未连接或会话已失效。</p>
                  <button class="sf-link" type="button" @click="router.push('/settings/integrations')">去连接 ↗</button>
                </template>
              </div>
            </div>
            <footer class="sf-card-actions">
              <button class="sf-quick-action" type="button" @click="openSalesforce('/lightning/o/Weekly_Plan__c/list')">查看全部 ↗</button>
              <button class="sf-quick-action ghost" type="button" @click="openSalesforce('/lightning/o/Weekly_Plan__c/new')">新建周计划</button>
              <button class="sf-quick-action ghost" type="button" :disabled="sfSections.plans.loading" @click="loadSalesforceSections">刷新</button>
            </footer>
          </article>
        </div>
      </section>

      <!-- OA：与「Salesforce」同样的形式 -->
      <section class="nc-source nc-source-oa" aria-labelledby="oa-wb-title">
        <header class="nc-source-head">
          <span class="nc-source-dot nc-dot-green"></span>
          <h4 class="nc-source-name" id="oa-wb-title">OA</h4>
        </header>
        <p class="nc-source-sub">
          公司 OA 的待办集中在这里处理，数据来自{{ oaConnected ? '已连接的 OA' : ' OA（未配置时点击下方"去配置"）' }}。
        </p>

        <!-- OA 连接面板：未配置时展开配置引导；已连接时收起成一行状态，
             鼠标移上去才浮现「断开连接」。断开后自动向下展开。
             注意：不能只判断 enabled —— 取数失败时 enabled 仍为 true（配置还在），
             若不把 reason 也纳入条件，用户会被"假已连接"挡住，无处重配。 -->
        <div class="sf-panel">
          <div
            class="sf-panel-bar"
            :class="{ collapsed: oaConnected && !oaReason }"
            @click="oaConnected && !oaReason ? router.push('/settings/integrations') : undefined"
          >
            <template v-if="oaConnected && !oaReason">
              <span class="sf-conn-ok"><span class="sf-conn-mark">✓</span>OA 已连接</span>
              <span class="sf-conn-hint">取数正常，下方卡片即为实时待办</span>
              <button
                class="sf-disconnect-btn"
                type="button"
                :disabled="oaDisconnecting"
                @click.stop="handleOaDisconnect"
              >{{ oaDisconnecting ? '断开中…' : '断开连接' }}</button>
            </template>
            <template v-else-if="oaConnected">
              <span class="sf-conn-ok is-warn"><span class="sf-conn-mark">!</span>OA 取数异常</span>
              <span class="sf-conn-hint">{{ oaReason || '请检查接口配置' }}</span>
            </template>
            <template v-else>
              <span class="sf-conn-ok"><span class="sf-conn-mark">○</span>OA 未配置</span>
              <span class="sf-conn-hint">需填写系统地址、待办接口路径与字段映射</span>
            </template>
          </div>

          <!-- 未配置时展开：引导去设置页填接口信息（OA 路径/字段映射项多，放设置页更合适） -->
          <div class="sf-panel-body" :class="{ collapsed: oaConnected && !oaReason }">
            <div class="sf-panel-body-inner">
              <div class="sf-quick-login">
                <p class="sf-quick-title">配置公司 OA 接入</p>
                <p class="sf-quick-sub">
                  OA 是泛微 e-cology，不同版本的待办接口路径与返回结构差异很大，系统不预设默认值。需在设置页填写<b>系统地址、待办接口路径、数据行路径与字段映射</b>；填好后此处自动收起并显示实时待办。
                </p>
                <div class="sf-actions">
                  <button class="sf-login-btn" type="button" @click="router.push('/settings/integrations')">去配置</button>
                  <button class="sf-settings-btn" type="button" @click="router.push('/settings/integrations')">消息互通设置</button>
                </div>
                <div v-if="oaReason" class="test-result fail"><span>✕</span>{{ oaReason }}</div>
              </div>
            </div>
          </div>
        </div>

        <div class="sf-wb-grid">
          <!-- OA 待办 -->
          <article class="sf-card" :class="{ live: oaConnected }">
            <header class="sf-card-head">
              <span class="sf-card-dot"></span>
              <h4>我的待办</h4>
              <span class="sf-card-count">{{ oaTodos.length }}</span>
            </header>
            <div class="sf-card-body">
              <div v-if="oaLoading" class="sf-card-loading">加载中…</div>
              <ul v-else-if="oaTodos.length" class="sf-list">
                <li
                  v-for="it in oaTodos"
                  :key="it.id"
                  class="sf-list-item"
                  @click="openUrl(it.url)"
                >
                  <div class="sf-list-copy">
                    <strong>{{ it.title }}</strong>
                    <span>{{ it.creator ? `发起人 ${it.creator}` : 'OA 待办' }}</span>
                  </div>
                  <span class="sf-due">{{ it.due || '无期限' }}</span>
                </li>
              </ul>
              <div v-else class="sf-card-empty">
                <template v-if="oaConnected">
                  当前没有待办事项。
                </template>
                <template v-else>
                  <p>公司 OA 尚未配置或取数失败。</p>
                  <button class="sf-link" type="button" @click="router.push('/settings/integrations')">去配置 ↗</button>
                </template>
              </div>
            </div>
            <footer class="sf-card-actions">
              <button class="sf-quick-action ghost" type="button" :disabled="oaLoading" @click="loadOaTodos">刷新</button>
            </footer>
          </article>
        </div>
      </section>
      </div>
    </section>


    <section class="module-panel">
      <div class="panel-heading">
        <h3>部门工作模块</h3>
        <!-- 管理员的账号入口：放在这里比只藏在头像菜单里更容易找到 -->
        <button v-if="isAdminUser" class="panel-link" type="button" @click="router.push('/account')">
          账号管理 ↗
        </button>
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
