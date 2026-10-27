import { api } from './http'

export type IntegrationSourceKey = 'dingtalk' | 'oa' | 'salesforce'

export interface IntegrationConfig {
  dingtalk: {
    corp_id: string
    app_key: string
    app_secret: string
    app_secret_configured: boolean
    app_secret_hint: string
    message_kinds: string
    user_id: string
    /** 行程创建人对照表，每行「行程标题=创建人」。钉钉接口不给每条日程的创建人，只能手填。 */
    trip_creator_map: string
  }
  oa: {
    base_url: string
    auth_mode: string
    username: string
    password: string
    password_configured: boolean
    password_hint: string
    token: string
    token_configured: boolean
    token_hint: string
    auth_header: string
    login_path: string
    todo_path: string
    todo_query_params: string
    rows_path: string
    field_map: string
    default_title: string
    timeout_seconds: number
    max_items: number
  }
  salesforce: {
    auth_mode: string
    instance_url: string
    api_version: string
    org_id: string
    username: string
    password: string
    password_configured: boolean
    password_hint: string
    totp_secret: string
    totp_secret_configured: boolean
    totp_secret_hint: string
    access_token: string
    access_token_configured: boolean
    access_token_hint: string
    client_id: string
    client_secret: string
    client_secret_configured: boolean
    client_secret_hint: string
    refresh_token: string
    refresh_token_configured: boolean
    refresh_token_hint: string
    token: string
    token_configured: boolean
    token_hint: string
    task_soql: string
    event_soql: string
    timeout_seconds: number
    max_items: number
  }
}

export interface IntegrationSourceFlag {
  source: IntegrationSourceKey
  label: string
  enabled: boolean
  missing: string[]
}

export interface IntegrationReadResponse {
  config: IntegrationConfig
  sources: IntegrationSourceFlag[]
  storage: string
}

export type IntegrationTestResult = { ok: boolean; detail: string }

export interface IntegrationTestResponse {
  dingtalk: IntegrationTestResult
  oa: IntegrationTestResult
  salesforce: IntegrationTestResult
}

export async function fetchIntegrations(): Promise<IntegrationReadResponse> {
  const response = await api.get<IntegrationReadResponse>('/integrations', { timeout: 15000 })
  return response.data
}

export async function saveIntegrations(payload: {
  dingtalk: Record<string, unknown>
  oa: Record<string, unknown>
  salesforce: Record<string, unknown>
}): Promise<IntegrationReadResponse> {
  const response = await api.put<IntegrationReadResponse>('/integrations', payload, { timeout: 15000 })
  return response.data
}

export async function testIntegrations(payload: {
  dingtalk: Record<string, unknown>
  oa: Record<string, unknown>
  salesforce: Record<string, unknown>
}): Promise<IntegrationTestResponse> {
  const response = await api.post<IntegrationTestResponse>('/integrations/test', payload, { timeout: 30000 })
  return response.data
}

export interface SalesforceLoginResult {
  ok: boolean
  detail: string
  token_hint?: string
  config?: IntegrationReadResponse['config']
  sources?: IntegrationReadResponse['sources']
}

/** 登录 OA 并保存账号（OA 只要账号密码，不需要动态令牌）。 */
export async function loginOa(payload: {
  base_url?: string
  username?: string
  password?: string
  login_path?: string
  todo_path?: string
  parser?: string
  xml_path?: string
  page_id?: string
}): Promise<SalesforceLoginResult> {
  const response = await api.post<SalesforceLoginResult>(
    '/integrations/oa/login',
    payload,
    { timeout: 60000 },
  )
  return response.data
}

/** 登录 Salesforce 并把会话 id 存回配置。 */
export async function loginSalesforce(payload: {
  instance_url?: string
  username?: string
  password?: string
  totp_secret?: string
  auth_mode?: string
  api_version?: string
  remember?: boolean
}): Promise<SalesforceLoginResult> {
  const response = await api.post<SalesforceLoginResult>(
    '/integrations/salesforce/login',
    payload,
    { timeout: 60000 },
  )
  return response.data
}

/** 断开 Salesforce：清掉本机保存的会话 id，前端连接面板会随之展开登录界面。 */
export async function disconnectSalesforce(): Promise<SalesforceLoginResult> {
  const response = await api.post<SalesforceLoginResult>(
    '/integrations/salesforce/disconnect',
    {},
    { timeout: 15000 },
  )
  return response.data
}

/** 首页 Salesforce：三个板块的通用返回结构。 */
export interface SfSectionResponse {
  instance_url: string
  connected: boolean
  error: string
  items: Record<string, unknown>[]
}

export async function fetchSfTodayTasks(): Promise<SfSectionResponse> {
  const response = await api.get<SfSectionResponse>('/integrations/salesforce/today-tasks', { timeout: 20000 })
  return response.data
}

/** 首页「今日日程」：我名下今天及以后结束的日程（对应 Salesforce 原生「今日事件」）。 */
export async function fetchSfMyEvents(): Promise<SfSectionResponse> {
  const response = await api.get<SfSectionResponse>('/integrations/salesforce/my-events', { timeout: 20000 })
  return response.data
}

/**
 * 首页「消息提醒」：钉钉日程里的提醒 + 消息人（组织者）与参与人。
 *
 * 钉钉没有开放收件箱查询接口，能拿到"提醒"的只有日程的 reminders 字段。
 * avatar_url 需要 Contact.User.Read 权限，未开通时为空串，
 * 前端用姓名首字兜底。
 */
export interface DingtalkReminderPerson {
  name: string
  id: string
  role: 'organizer' | 'attendee'
  is_self: string
  avatar_url: string
}

export interface DingtalkReminder {
  id: string
  title: string
  start: string
  all_day: boolean
  remind_minutes: string
  remind_methods: string[]
  status: string
  people: DingtalkReminderPerson[]
}

export interface DingtalkRemindersResponse {
  connected: boolean
  error: string
  note: string
  items: DingtalkReminder[]
}

export async function fetchDingtalkReminders(userId = ''): Promise<DingtalkRemindersResponse> {
  const response = await api.get<DingtalkRemindersResponse>('/integrations/dingtalk/reminders', {
    params: { user_id: userId || undefined },
    timeout: 25000,
  })
  return response.data
}

/** 首页「OA」的待办列表。 */
export interface OaTodoItem {
  id: string
  title: string
  creator: string
  due: string
  priority: string
  url: string
  detail: string
}

export interface OaTodosResponse {
  instance_url: string
  connected: boolean
  error: string
  items: OaTodoItem[]
}

export async function fetchOaTodos(): Promise<OaTodosResponse> {
  const response = await api.get<OaTodosResponse>('/integrations/oa/todos', { timeout: 20000 })
  return response.data
}

/** 断开公司 OA：清掉凭据与接口路径，OA随之展开配置界面。 */
export async function disconnectOa(): Promise<{ ok: boolean; detail: string }> {
  const response = await api.post<{ ok: boolean; detail: string }>(
    '/integrations/oa/disconnect',
    {},
    { timeout: 15000 },
  )
  return response.data
}

export async function fetchSfApprovals(): Promise<SfSectionResponse> {
  const response = await api.get<SfSectionResponse>('/integrations/salesforce/approvals', { timeout: 20000 })
  return response.data
}

export async function fetchSfWeeklyPlans(): Promise<SfSectionResponse> {
  const response = await api.get<SfSectionResponse>('/integrations/salesforce/weekly-plans', { timeout: 20000 })
  return response.data
}

/**
 * 申请 Salesforce 免登跳转令牌（给 API 客户端用）。
 *
 * 后端自己登录拿到的 sid 只在服务器上，浏览器没有会话 —— 直接开 Salesforce
 * 原生页会被踢回登录页。这个令牌换来的 frontdoor 地址让浏览器自动认领会话。
 *
 * ⚠️ 前端点按钮**不用**这个接口：走 `sfLaunchUrl(path)` 一次同步导航更稳
 * （异步两步之间任何一步失败都会留下空白标签页）。
 */
export async function requestSfLaunch(path: string): Promise<{ ok: boolean; token: string; expires_in: number }> {
  const response = await api.post<{ ok: boolean; token: string; expires_in: number }>(
    '/integrations/salesforce/launch',
    { path },
    { timeout: 15000 },
  )
  return response.data
}

/** 免登跳转地址：一次同步导航，后端 302 到 Salesforce frontdoor。 */
export function sfLaunchUrl(path: string): string {
  return `/api/integrations/salesforce/launch?path=${encodeURIComponent(path)}`
}

/** 钉钉事件推送采集器状态（自动记录每条日程的创建人） */
export interface DingtalkCollectorStatus {
  ok: boolean
  collector: {
    running: boolean
    connected: boolean
    last_error: string
    last_event_at: string
    recorded_count: number
    last_connect_at: string
  }
  recorded_total: number
}

export async function fetchDingtalkCollector(): Promise<DingtalkCollectorStatus> {
  const response = await api.get<DingtalkCollectorStatus>('/integrations/dingtalk/collector', { timeout: 10000 })
  return response.data
}
