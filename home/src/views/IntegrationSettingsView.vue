<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  fetchIntegrations,
  saveIntegrations,
  testIntegrations,
  type IntegrationConfig,
  type IntegrationSourceFlag,
  type IntegrationTestResult,
  fetchDingtalkCollector,
  type DingtalkCollectorStatus,
} from '../api/integrations'
import { resolveDingTalkUserId } from '../api/messages'
import { loginSalesforce } from '../api/integrations'

const router = useRouter()

const loading = ref(true)
const saving = ref(false)
const testing = ref(false)
const loadError = ref('')
const saveMessage = ref('')
const saveError = ref('')
const storagePath = ref('')
const sourceFlags = ref<IntegrationSourceFlag[]>([])
const testResults = ref<Record<string, IntegrationTestResult>>({})

// 各密码类字段的展示状态。后端只回传「已配置/是否配置 + 脱敏提示」不回传明文，
// 所以这里显式记录三件事：是否已保存、脱敏提示（长度+末4位）、是否处于「编辑中」。
//
// 关键：Secret 框默认是「只读 + 显示掩码」，用户必须点「修改」才能输入。
// 这样解决两个问题：
//   1. 刷新后不再是空框，能看到"确实存着"（已保存 · 64 位 · 末 4 位 HcAk）；
//   2. 断掉 Chrome 密码管理器的自动填充——它会把浏览器里的旧密码塞进 input，
//      用户一点保存就把旧值写回去，把新配的 Secret 覆盖掉。
const secretMeta = reactive<Record<string, { configured: boolean; hint: string; editing: boolean }>>({
  dingtalk_app_secret: { configured: false, hint: '', editing: false },
  oa_password: { configured: false, hint: '', editing: false },
  oa_token: { configured: false, hint: '', editing: false },
  salesforce_access_token: { configured: false, hint: '', editing: false },
  salesforce_client_secret: { configured: false, hint: '', editing: false },
  salesforce_token: { configured: false, hint: '', editing: false },
  salesforce_password: { configured: false, hint: '', editing: false },
  salesforce_totp_secret: { configured: false, hint: '', editing: false },
  salesforce_refresh_token: { configured: false, hint: '', editing: false },
})

// key -> 表单里的实际字段路径
const SECRET_PATHS: Record<string, [string, string]> = {
  dingtalk_app_secret: ['dingtalk', 'app_secret'],
  oa_password: ['oa', 'password'],
  oa_token: ['oa', 'token'],
  salesforce_access_token: ['salesforce', 'access_token'],
  salesforce_client_secret: ['salesforce', 'client_secret'],
  salesforce_token: ['salesforce', 'token'],
  salesforce_password: ['salesforce', 'password'],
  salesforce_totp_secret: ['salesforce', 'totp_secret'],
  salesforce_refresh_token: ['salesforce', 'refresh_token'],
}

function getSecretField(key: string): string {
  const [section, field] = SECRET_PATHS[key]
  const bag = form[section as keyof typeof form] as Record<string, string>
  return bag[field] ?? ''
}

function setSecretField(key: string, value: string) {
  const [section, field] = SECRET_PATHS[key]
  const bag = form[section as keyof typeof form] as Record<string, string>
  bag[field] = value
}

function secretValue(key: string): string {
  const meta = secretMeta[key]
  if (meta.editing) return getSecretField(key)
  return meta.configured ? '••••••••••••••••' : ''
}

function onSecretInput(key: string, event: Event) {
  setSecretField(key, (event.target as HTMLInputElement).value)
}

function toggleSecretEdit(key: string) {
  const meta = secretMeta[key]
  meta.editing = !meta.editing
  if (meta.editing) {
    // 进入编辑态：清空当前值，让用户输入新的；留空提交 = 不修改（后端会保留原值）
    setSecretField(key, '')
  }
}

const form = reactive({
  dingtalk: {
    corp_id: '',
    app_key: '',
    app_secret: '',
    message_kinds: 'todo,calendar,trip',
    user_id: '',
    trip_creator_map: '',
  },
  oa: {
    base_url: '',
    auth_mode: 'password',
    username: '',
    password: '',
    token: '',
    auth_header: '',
    login_path: '',
    todo_path: '',
    todo_query_params: '',
    rows_path: '',
    field_map: '',
    default_title: 'OA 待办',
    timeout_seconds: 10,
    max_items: 30,
  },
  salesforce: {
    instance_url: '',
    access_token: '',
    client_id: '',
    client_secret: '',
    token: '',
  },
})

const dingtalkKinds = [
  { key: 'todo', label: '待办任务', hint: 'v1.0/todo/users/{unionId}/org/tasks/query' },
  { key: 'calendar', label: '个人日程', hint: 'v1.0/calendar/users/{unionId}/calendars/primary/events' },
  { key: 'trip', label: '部门行程', hint: 'v1.0/calendar/users/{unionId}/calendars/{calendarId}/events' },
]

const selectedKinds = computed(() =>
  form.dingtalk.message_kinds
    .split(',')
    .map((k) => k.trim())
    .filter(Boolean),
)

function toggleKind(key: string) {
  const set = new Set(selectedKinds.value)
  if (set.has(key)) {
    set.delete(key)
  } else {
    set.add(key)
  }
  form.dingtalk.message_kinds = dingtalkKinds.map((k) => k.key).filter((k) => set.has(k)).join(',')
}

const oaFieldMapValid = computed(() => {
  const raw = form.oa.field_map.trim()
  if (!raw) return true
  try {
    const parsed = JSON.parse(raw)
    return typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed)
  } catch {
    return false
  }
})

const oaFieldMapHasTitle = computed(() => {
  if (!form.oa.field_map.trim()) return false
  try {
    const parsed = JSON.parse(form.oa.field_map)
    return Boolean(parsed.title)
  } catch {
    return false
  }
})

function flagOf(source: string): IntegrationSourceFlag | undefined {
  return sourceFlags.value.find((item) => item.source === source)
}

const mobileInput = ref('')
const useridLooking = ref(false)
const sfLogging = ref(false)
const sfLoginMessage = ref('')
const sfLoginError = ref('')
const useridMessage = ref('')

async function handleResolveUserId() {
  const mobile = mobileInput.value.trim()
  if (!mobile) {
    useridMessage.value = '请先填写手机号。'
    return
  }
  useridLooking.value = true
  useridMessage.value = ''
  try {
    // 把表单里当前的凭据一起传过去：用户可能刚填了 Secret 还没保存，
    // 不传的话后端只能读到旧配置，会报"缺少参数 appsecret"。
    const result = await resolveDingTalkUserId(mobile, {
      app_key: form.dingtalk.app_key.trim(),
      app_secret: form.dingtalk.app_secret,
      corp_id: form.dingtalk.corp_id.trim(),
    })
    if (result.ok && result.user_id) {
      form.dingtalk.user_id = result.user_id
      useridMessage.value = `已获取 userid：${result.user_id}，记得点「保存配置」。`
    } else {
      useridMessage.value = result.detail || '获取 userid 失败。'
    }
  } catch (error) {
    useridMessage.value = '获取 userid 请求失败，请确认后端已启动。'
    console.error(error)
  } finally {
    useridLooking.value = false
  }
}

// 钉钉事件推送采集器状态：自动记录每条日程的创建人
const collector = ref<DingtalkCollectorStatus | null>(null)
async function loadCollectorStatus() {
  try {
    collector.value = await fetchDingtalkCollector()
  } catch (error) {
    console.error(error)
  }
}

function applyConfig(config: IntegrationConfig) {
  form.dingtalk.corp_id = config.dingtalk.corp_id ?? ''
  form.dingtalk.app_key = config.dingtalk.app_key ?? ''
  form.dingtalk.message_kinds = config.dingtalk.message_kinds || 'todo,calendar,trip'
  form.dingtalk.user_id = config.dingtalk.user_id ?? ''
  form.dingtalk.trip_creator_map = config.dingtalk.trip_creator_map ?? ''
  loadCollectorStatus()

  // 记录哪些密码已保存 + 脱敏提示，供界面显示"已保存"标记与掩码
  secretMeta.dingtalk_app_secret.configured = Boolean(config.dingtalk.app_secret_configured)
  secretMeta.dingtalk_app_secret.hint = config.dingtalk.app_secret_hint ?? ''
  secretMeta.oa_password.configured = Boolean(config.oa.password_configured)
  secretMeta.oa_password.hint = config.oa.password_hint ?? ''
  secretMeta.oa_token.configured = Boolean(config.oa.token_configured)
  secretMeta.oa_token.hint = config.oa.token_hint ?? ''
  secretMeta.salesforce_access_token.configured = Boolean(config.salesforce.access_token_configured)
  secretMeta.salesforce_access_token.hint = config.salesforce.access_token_hint ?? ''
  secretMeta.salesforce_client_secret.configured = Boolean(config.salesforce.client_secret_configured)
  secretMeta.salesforce_client_secret.hint = config.salesforce.client_secret_hint ?? ''
  secretMeta.salesforce_token.configured = Boolean(config.salesforce.token_configured)
  secretMeta.salesforce_token.hint = config.salesforce.token_hint ?? ''
  secretMeta.salesforce_password.configured = Boolean(config.salesforce.password_configured)
  secretMeta.salesforce_password.hint = config.salesforce.password_hint ?? ''
  secretMeta.salesforce_totp_secret.configured = Boolean(config.salesforce.totp_secret_configured)
  secretMeta.salesforce_totp_secret.hint = config.salesforce.totp_secret_hint ?? ''
  secretMeta.salesforce_refresh_token.configured = Boolean(config.salesforce.refresh_token_configured)
  secretMeta.salesforce_refresh_token.hint = config.salesforce.refresh_token_hint ?? ''

  // 无论加载还是保存完成后，密码框都回到「只读 + 掩码」状态
  for (const key of Object.keys(secretMeta)) {
    secretMeta[key].editing = false
    // 同时清掉表单里残留的明文，避免再次保存时把明文又提交一遍
    setSecretField(key, '')
  }

  form.oa.base_url = config.oa.base_url ?? ''
  form.oa.auth_mode = config.oa.auth_mode || 'password'
  form.oa.username = config.oa.username ?? ''
  form.oa.auth_header = config.oa.auth_header ?? ''
  form.oa.login_path = config.oa.login_path ?? ''
  form.oa.todo_path = config.oa.todo_path ?? ''
  form.oa.todo_query_params = config.oa.todo_query_params ?? ''
  form.oa.rows_path = config.oa.rows_path ?? ''
  form.oa.field_map = config.oa.field_map ?? ''
  form.oa.default_title = config.oa.default_title || 'OA 待办'
  form.oa.timeout_seconds = config.oa.timeout_seconds ?? 10
  form.oa.max_items = config.oa.max_items ?? 30

  form.salesforce.auth_mode = config.salesforce.auth_mode || 'totp'
  form.salesforce.instance_url = config.salesforce.instance_url ?? ''
  form.salesforce.api_version = config.salesforce.api_version || 'v62.0'
  form.salesforce.org_id = config.salesforce.org_id ?? ''
  form.salesforce.username = config.salesforce.username ?? ''
  form.salesforce.client_id = config.salesforce.client_id ?? ''
}

async function loadSettings() {
  loading.value = true
  loadError.value = ''
  try {
    const response = await fetchIntegrations()
    applyConfig(response.config)
    sourceFlags.value = response.sources
    storagePath.value = response.storage
  } catch (error) {
    loadError.value = '无法读取配置，请确认后端服务已启动。'
    console.error(error)
  } finally {
    loading.value = false
  }
}

async function handleSave() {
  if (!oaFieldMapValid.value) {
    saveError.value = 'OA 字段映射不是合法的 JSON 对象。'
    return
  }
  saving.value = true
  saveMessage.value = ''
  saveError.value = ''
  try {
    const response = await saveIntegrations({
      dingtalk: { ...form.dingtalk },
      oa: { ...form.oa },
      salesforce: { ...form.salesforce },
    })
    sourceFlags.value = response.sources
    applyConfig(response.config)
    saveMessage.value = '配置已保存，首页刷新后立即生效。'
  } catch (error) {
    saveError.value = '保存失败，请检查后端服务状态。'
    console.error(error)
  } finally {
    saving.value = false
  }
}

/** 登录 Salesforce：把当前表单里的凭据发后端换会话 id 并存回配置。 */
async function handleSalesforceLogin() {
  sfLogging.value = true
  sfLoginMessage.value = ''
  sfLoginError.value = ''
  try {
    const result = await loginSalesforce({
      instance_url: form.salesforce.instance_url.trim(),
      username: form.salesforce.username.trim(),
      password: form.salesforce.password,
      totp_secret: form.salesforce.totp_secret,
      auth_mode: form.salesforce.auth_mode,
      api_version: form.salesforce.api_version.trim(),
      // 设置页是"配置并保存凭据"的语义，登录后保留账号密码（与首页勾选"记住"一致）
      remember: true,
    })
    if (result.ok) {
      // 用后端返回的文案，别自己拼——后端会区分
      // 「新登录成功」和「现有会话仍可用」两种情况
      sfLoginMessage.value = result.detail || '登录成功'
      if (result.config) applyConfig(result.config)
      if (result.sources) sourceFlags.value = result.sources
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

async function handleTest() {
  testing.value = true
  testResults.value = {}
  try {
    testResults.value = await testIntegrations({
      dingtalk: { ...form.dingtalk },
      oa: { ...form.oa },
      salesforce: { ...form.salesforce },
    })
  } catch (error) {
    testResults.value = {
      dingtalk: { ok: false, detail: '自检请求失败' },
      oa: { ok: false, detail: '自检请求失败' },
      salesforce: { ok: false, detail: '自检请求失败' },
    }
    console.error(error)
  } finally {
    testing.value = false
  }
}

onMounted(loadSettings)
</script>

<template>
  <section class="settings-shell">
    <header class="settings-head">
      <div>
        <p class="section-kicker">INTEGRATIONS · SETTINGS</p>
        <h2>消息互通设置</h2>
        <p class="settings-subtitle">
          配置钉钉、Salesforce、公司 OA 三个来源的凭据与接口参数。保存后首页「Notifications Center」立即按新配置拉取，无需重启后端。
        </p>
      </div>
      <div class="settings-actions">
        <button class="secondary-button" type="button" :disabled="testing" @click="handleTest">
          {{ testing ? '检测中…' : '连通性检测' }}
        </button>
        <button class="primary-button" type="button" :disabled="saving" @click="handleSave">
          {{ saving ? '保存中…' : '保存配置' }}
        </button>
        <button class="ghost-button" type="button" @click="router.push('/')">返回首页 ↗</button>
      </div>
    </header>

    <div v-if="loadError" class="notice-bar warning"><span class="notice-mark">!</span>{{ loadError }}</div>
    <div v-if="saveMessage" class="notice-bar info"><span class="notice-mark">i</span>{{ saveMessage }}</div>
    <div v-if="saveError" class="notice-bar warning"><span class="notice-mark">!</span>{{ saveError }}</div>

    <div v-if="loading" class="settings-loading">读取配置中…</div>

    <template v-else>
      <!-- 钉钉 -->
      <article class="config-card">
        <header class="config-card-head">
          <div class="config-title">
            <span class="config-icon tone-blue">
              <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
                <path d="M12 3l8 4.5v9L12 21l-8-4.5v-9L12 3z" /><path d="M12 12v9M12 12l8-4.5M12 12L4 7.5" />
              </svg>
            </span>
            <div>
              <h3>钉钉</h3>
              <p>企业内部应用凭据，用于拉取待办、日程与部门行程。</p>
            </div>
          </div>
          <span class="config-status" :class="{ live: flagOf('dingtalk')?.enabled }">
            {{ flagOf('dingtalk')?.enabled ? '已配置' : '未配置' }}
          </span>
        </header>

        <div v-if="flagOf('dingtalk')?.missing.length" class="config-hint">
          还缺：{{ flagOf('dingtalk')?.missing.join('、') }}
        </div>

        <div class="field-grid">
          <label class="field">
            <span>Client ID（原 AppKey）</span>
            <input v-model="form.dingtalk.app_key" type="text" placeholder="dingaadldrx3nvfxjceo" />
          </label>
          <label class="field">
            <span>
              Client Secret（原 AppSecret）
              <em v-if="secretMeta.dingtalk_app_secret.configured" class="saved-flag">已保存</em>
            </span>
            <div class="secret-row">
              <input
                :value="secretValue('dingtalk_app_secret')"
                :type="secretMeta.dingtalk_app_secret.editing ? 'password' : 'text'"
                :readonly="!secretMeta.dingtalk_app_secret.editing"
                :placeholder="secretMeta.dingtalk_app_secret.editing ? '粘贴新的 Client Secret' : '尚未配置'"
                autocomplete="new-password"
                @input="onSecretInput('dingtalk_app_secret', $event)"
              />
              <button
                type="button"
                class="ghost-button secret-edit-btn"
                @click="toggleSecretEdit('dingtalk_app_secret')"
              >
                {{ secretMeta.dingtalk_app_secret.editing ? '取消' : '修改' }}
              </button>
            </div>
            <small v-if="secretMeta.dingtalk_app_secret.configured && !secretMeta.dingtalk_app_secret.editing">
              已保存（{{ secretMeta.dingtalk_app_secret.hint }}），保持不变即可；点「修改」可覆盖
            </small>
            <small v-else-if="secretMeta.dingtalk_app_secret.editing">
              留空直接保存 = 保留原有值；从钉钉开放平台「凭证与基础信息」复制完整值
            </small>
            <small v-else>从钉钉开放平台「凭证与基础信息」复制完整值</small>
          </label>
          <label class="field">
            <span>CorpId（可选）</span>
            <input v-model="form.dingtalk.corp_id" type="text" placeholder="ding_xxxxxxxx，留空也能用" />
            <small>开发者后台首页右侧可查。无此权限时留空，系统走旧版通道</small>
          </label>
          <label class="field">
            <span>默认 userid（可选）</span>
            <input v-model="form.dingtalk.user_id" type="text" placeholder="留空则由前端免登换取" />
            <small>待办/日程按人查询，没有 userid 拉不到数据</small>
          </label>
          <label class="field">
            <span>行程创建人对照表（可选）</span>
            <textarea
              v-model="form.dingtalk.trip_creator_map"
              rows="5"
              placeholder="每行一条：&#10;眉山中6s调研与文化墙沟通=杨娜&#10;绵阳人民大家检验回访、答疑=孟鹏杰"
            ></textarea>
            <small>
              显示在首页「部门行程」里的创建人。钉钉接口不返回每条日程的创建人，
              系统会自动在同事的主日历里找这条行程的副本来判定（副本的主人即创建人）；
              判定不出来的行程，在这里按「行程标题=创建人」逐行指定。识别不到就不显示，绝不瞎猜
            </small>
            <small class="collector-state">
              自动采集：
              <template v-if="collector?.collector?.connected">
                <em class="is-ok">已连接钉钉事件推送</em>，新建行程的创建人会自动记录
                （已记录 {{ collector.recorded_total }} 条）
              </template>
              <template v-else-if="collector?.collector?.last_error">
                <em class="is-warn">未连接：{{ collector.collector.last_error }}</em>
              </template>
              <template v-else>未连接</template>
              <br />
              需要在钉钉开发者后台：开发配置 → 事件订阅 → <b>Stream 模式推送</b> →
              订阅事件类型「日程变更」（calendar_event_change）
            </small>
          </label>
          <label class="field">
            <span>手机号查 userid（没有 CorpId 时用）</span>
            <input v-model="mobileInput" type="text" placeholder="组织内登记的手机号" />
            <small>用手机号换取 userid，拿到后自动填入上一项</small>
          </label>
        </div>
        <div class="field-actions">
          <button type="button" class="ghost-button" :disabled="useridLooking" @click="handleResolveUserId">
            {{ useridLooking ? '查询中…' : '用手机号查 userid' }}
          </button>
          <span v-if="useridMessage" class="field-hint">{{ useridMessage }}</span>
        </div>

        <div class="field-block">
          <span class="field-label">拉取的消息类型</span>
          <div class="kind-toggles">
            <button
              v-for="kind in dingtalkKinds"
              :key="kind.key"
              class="kind-toggle"
              :class="{ active: selectedKinds.includes(kind.key) }"
              type="button"
              @click="toggleKind(kind.key)"
            >
              <strong>{{ kind.label }}</strong>
              <small>{{ kind.hint }}</small>
            </button>
          </div>
        </div>

        <div v-if="testResults.dingtalk" class="test-result" :class="testResults.dingtalk.ok ? 'ok' : 'fail'">
          <span>{{ testResults.dingtalk.ok ? '✓' : '✕' }}</span>{{ testResults.dingtalk.detail }}
        </div>
      </article>

      <!-- OA -->
      <article class="config-card">
        <header class="config-card-head">
          <div class="config-title">
            <span class="config-icon tone-green">
              <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
                <path d="M7 3h8l4 4v14H7z" /><path d="M15 3v4h4" /><path d="M10 12h6M10 16h6" />
              </svg>
            </span>
            <div>
              <h3>公司 OA</h3>
              <p>泛微 e-cology（oa.maccura.com）。接口路径需抓包确认后填写，系统不预设默认值。</p>
            </div>
          </div>
          <span class="config-status" :class="{ live: flagOf('oa')?.enabled }">
            {{ flagOf('oa')?.enabled ? '已配置' : '未配置' }}
          </span>
        </header>

        <div v-if="flagOf('oa')?.missing.length" class="config-hint">
          还缺：{{ flagOf('oa')?.missing.join('、') }}
        </div>

        <div class="field-grid">
          <label class="field">
            <span>系统地址</span>
            <input v-model="form.oa.base_url" type="text" placeholder="https://oa.maccura.com" />
          </label>
          <label class="field">
            <span>鉴权方式</span>
            <select v-model="form.oa.auth_mode">
              <option value="password">账号密码登录</option>
              <option value="token">固定令牌 / Cookie</option>
            </select>
          </label>
          <template v-if="form.oa.auth_mode === 'password'">
            <label class="field">
              <span>账号</span>
              <input v-model="form.oa.username" type="text" placeholder="OA 登录账号" />
            </label>
            <label class="field">
              <span>
                密码
                <em v-if="secretMeta.oa_password.configured" class="saved-flag">已保存</em>
              </span>
              <div class="secret-row">
                <input
                  :value="secretValue('oa_password')"
                  :type="secretMeta.oa_password.editing ? 'password' : 'text'"
                  :readonly="!secretMeta.oa_password.editing"
                  :placeholder="secretMeta.oa_password.editing ? '粘贴新的 OA 密码' : '尚未配置'"
                  autocomplete="new-password"
                  @input="onSecretInput('oa_password', $event)"
                />
                <button type="button" class="ghost-button secret-edit-btn" @click="toggleSecretEdit('oa_password')">
                  {{ secretMeta.oa_password.editing ? '取消' : '修改' }}
                </button>
              </div>
              <small v-if="secretMeta.oa_password.configured && !secretMeta.oa_password.editing">
                已保存（{{ secretMeta.oa_password.hint }}），保持不变即可
              </small>
              <small v-else>OA 登录密码</small>
            </label>
            <label class="field field-wide">
              <span>登录接口路径</span>
              <input v-model="form.oa.login_path" type="text" placeholder="login/CheckLogin.jsp" />
            </label>
          </template>
          <template v-else>
            <label class="field">
              <span>
                令牌 / Cookie
                <em v-if="secretMeta.oa_token.configured" class="saved-flag">已保存</em>
              </span>
              <div class="secret-row">
                <input
                  :value="secretValue('oa_token')"
                  :type="secretMeta.oa_token.editing ? 'password' : 'text'"
                  :readonly="!secretMeta.oa_token.editing"
                  :placeholder="secretMeta.oa_token.editing ? '粘贴新的 Cookie / Token' : '尚未配置'"
                  autocomplete="new-password"
                  @input="onSecretInput('oa_token', $event)"
                />
                <button type="button" class="ghost-button secret-edit-btn" @click="toggleSecretEdit('oa_token')">
                  {{ secretMeta.oa_token.editing ? '取消' : '修改' }}
                </button>
              </div>
              <small v-if="secretMeta.oa_token.configured && !secretMeta.oa_token.editing">
                已保存（{{ secretMeta.oa_token.hint }}），保持不变即可
              </small>
              <small v-else>浏览器 F12 复制完整 Cookie</small>
            </label>
            <label class="field">
              <span>自定义请求头（可选）</span>
              <input v-model="form.oa.auth_header" type="text" placeholder="如 X-Token，留空则当 Cookie 发送" />
            </label>
          </template>
          <label class="field field-wide">
            <span>待办接口路径</span>
            <input v-model="form.oa.todo_path" type="text" placeholder="如 api/workflow/paService.do" />
            <small>浏览器登录 OA 后按 F12 抓「我的待办」请求，取其路径</small>
          </label>
          <label class="field field-wide">
            <span>查询参数</span>
            <input v-model="form.oa.todo_query_params" type="text" placeholder="userId=xxx&pageSize=20" />
          </label>
          <label class="field">
            <span>列表所在路径</span>
            <input v-model="form.oa.rows_path" type="text" placeholder="result.todos" />
          </label>
          <label class="field">
            <span>默认标题</span>
            <input v-model="form.oa.default_title" type="text" placeholder="OA 待办" />
          </label>
        </div>

        <label class="field field-block">
          <span>字段映射（JSON）</span>
          <textarea
            v-model="form.oa.field_map"
            rows="3"
            placeholder='{"title":"subject","due":"finishTime","creator":"creator","id":"workflowid","url":"link","priority":"urgency"}'
          ></textarea>
          <small v-if="!oaFieldMapValid" class="field-error">不是合法的 JSON 对象</small>
          <small v-else-if="!oaFieldMapHasTitle" class="field-error">必须包含 title 字段，否则无法定位待办标题</small>
          <small v-else>至少要有 title；其余字段按实际响应结构填</small>
        </label>

        <div v-if="testResults.oa" class="test-result" :class="testResults.oa.ok ? 'ok' : 'fail'">
          <span>{{ testResults.oa.ok ? '✓' : '✕' }}</span>{{ testResults.oa.detail }}
        </div>
      </article>

      <!-- Salesforce -->
      <article class="config-card">
        <header class="config-card-head">
          <div class="config-title">
            <span class="config-icon tone-orange">
              <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
                <path d="M4 18c3-1 5-3 6-6s3-5 6-6c2 0 4 1 4 3s-1 3-3 3-3-1-3-3" />
              </svg>
            </span>
            <div>
              <h3>Salesforce</h3>
              <p>拉取待办任务与日程。账号已启用双因素认证，默认走「密码 + 动态口令」登录。</p>
            </div>
          </div>
          <span class="config-status" :class="{ live: flagOf('salesforce')?.enabled }">
            {{ flagOf('salesforce')?.enabled ? '已配置' : '未配置' }}
          </span>
        </header>

        <div class="field-grid">
          <label class="field">
            <span>鉴权方式</span>
            <select v-model="form.salesforce.auth_mode">
              <option value="totp">密码 + 动态口令（双因素认证）</option>
              <option value="oauth">OAuth（Connected App）</option>
              <option value="session">直接使用已保存的会话</option>
            </select>
            <small v-if="form.salesforce.auth_mode === 'totp'">
              走账号登录 + 动态口令，适配已开双因素认证的实例
            </small>
            <small v-else-if="form.salesforce.auth_mode === 'oauth'">
              由管理员建 Connected App 后用 Client ID/Secret 换 token
            </small>
            <small v-else>直接用已保存的会话 id，会话过期后需重新登录</small>
          </label>
          <label class="field">
            <span>实例地址（API 主机）</span>
            <input v-model="form.salesforce.instance_url" type="text" placeholder="https://xxx.my.salesforce.com" />
            <small>填 My Domain，不要填 lightning 界面地址</small>
          </label>
          <label class="field">
            <span>用户名</span>
            <input v-model="form.salesforce.username" type="text" placeholder="组织内邮箱" />
          </label>
          <label class="field">
            <span>API 版本</span>
            <input v-model="form.salesforce.api_version" type="text" placeholder="v62.0" />
          </label>
          <label class="field">
            <span>
              密码
              <em v-if="secretMeta.salesforce_password.configured" class="saved-flag">已保存</em>
            </span>
            <div class="secret-row">
              <input
                :value="secretValue('salesforce_password')"
                :type="secretMeta.salesforce_password.editing ? 'password' : 'text'"
                :readonly="!secretMeta.salesforce_password.editing"
                :placeholder="secretMeta.salesforce_password.editing ? '粘贴新的密码' : '尚未配置'"
                autocomplete="new-password"
                @input="onSecretInput('salesforce_password', $event)"
              />
              <button
                type="button"
                class="ghost-button secret-edit-btn"
                @click="toggleSecretEdit('salesforce_password')"
              >
                {{ secretMeta.salesforce_password.editing ? '取消' : '修改' }}
              </button>
            </div>
            <small v-if="secretMeta.salesforce_password.configured && !secretMeta.salesforce_password.editing">
              已保存（{{ secretMeta.salesforce_password.hint }}）
            </small>
          </label>
          <label class="field">
            <span>
              动态口令（腾讯验证器 6 位码）
              <em v-if="secretMeta.salesforce_totp_secret.configured" class="saved-flag">已保存</em>
            </span>
            <div class="secret-row">
              <input
                :value="secretValue('salesforce_totp_secret')"
                :type="secretMeta.salesforce_totp_secret.editing ? 'password' : 'text'"
                :readonly="!secretMeta.salesforce_totp_secret.editing"
                :placeholder="secretMeta.salesforce_totp_secret.editing ? '粘贴 6 位动态码或 base32 密钥' : '尚未配置'"
                autocomplete="new-password"
                @input="onSecretInput('salesforce_totp_secret', $event)"
              />
              <button
                type="button"
                class="ghost-button secret-edit-btn"
                @click="toggleSecretEdit('salesforce_totp_secret')"
              >
                {{ secretMeta.salesforce_totp_secret.editing ? '取消' : '修改' }}
              </button>
            </div>
            <small v-if="secretMeta.salesforce_totp_secret.configured && !secretMeta.salesforce_totp_secret.editing">
              已保存（{{ secretMeta.salesforce_totp_secret.hint }}）
            </small>
            <small v-else>
              填腾讯身份验证器当前的 6 位动态码（30 秒刷新，登录时用一次即可，不会落盘），
              也可填验证器 App 设置页里的 base32「手动输入密钥」用于自动重登。
            </small>
          </label>
          <label class="field">
            <span>Client ID</span>
            <input v-model="form.salesforce.client_id" type="text" placeholder="Connected App Consumer Key" />
          </label>
          <label class="field">
            <span>
              Client Secret
              <em v-if="secretMeta.salesforce_client_secret.configured" class="saved-flag">已保存</em>
            </span>
            <div class="secret-row">
              <input
                :value="secretValue('salesforce_client_secret')"
                :type="secretMeta.salesforce_client_secret.editing ? 'password' : 'text'"
                :readonly="!secretMeta.salesforce_client_secret.editing"
                :placeholder="secretMeta.salesforce_client_secret.editing ? '粘贴新的 Client Secret' : '尚未配置'"
                autocomplete="new-password"
                @input="onSecretInput('salesforce_client_secret', $event)"
              />
              <button
                type="button"
                class="ghost-button secret-edit-btn"
                @click="toggleSecretEdit('salesforce_client_secret')"
              >
                {{ secretMeta.salesforce_client_secret.editing ? '取消' : '修改' }}
              </button>
            </div>
            <small v-if="secretMeta.salesforce_client_secret.configured && !secretMeta.salesforce_client_secret.editing">
              已保存（{{ secretMeta.salesforce_client_secret.hint }}）
            </small>
          </label>
          <label class="field">
            <span>
              Access Token / 自研令牌
              <em v-if="secretMeta.salesforce_access_token.configured || secretMeta.salesforce_token.configured" class="saved-flag">已保存</em>
            </span>
            <div class="secret-row">
              <input
                :value="secretValue('salesforce_access_token')"
                :type="secretMeta.salesforce_access_token.editing ? 'password' : 'text'"
                :readonly="!secretMeta.salesforce_access_token.editing"
                :placeholder="secretMeta.salesforce_access_token.editing ? '粘贴新的 Access Token' : '尚未配置'"
                autocomplete="new-password"
                @input="onSecretInput('salesforce_access_token', $event)"
              />
              <button
                type="button"
                class="ghost-button secret-edit-btn"
                @click="toggleSecretEdit('salesforce_access_token')"
              >
                {{ secretMeta.salesforce_access_token.editing ? '取消' : '修改' }}
              </button>
            </div>
            <small v-if="secretMeta.salesforce_access_token.configured && !secretMeta.salesforce_access_token.editing">
              已保存（{{ secretMeta.salesforce_access_token.hint }}）
            </small>
          </label>
        </div>

        <div class="field-block">
          <span class="field-label">登录并获取 Token</span>
          <p class="config-hint" style="margin: 0 0 10px">
            填好上面的账号、密码，再把腾讯身份验证器当前的 6 位动态码填进「动态口令」，
            点登录即可。后端会自动走完「密码 → 动态口令 → 会话」并把会话存下来，之后首页直接用它拉数据；
            会话过期后再点一次就行，不用手填。
          </p>
          <div class="secret-row">
            <button
              type="button"
              class="primary-button"
              :disabled="sfLogging"
              @click="handleSalesforceLogin"
            >
              {{ sfLogging ? '登录中…' : '登录并获取 Token' }}
            </button>
            <button
              type="button"
              class="secondary-button"
              :disabled="sfLogging"
              @click="handleTest"
            >
              测试连接
            </button>
          </div>
          <div v-if="sfLoginMessage" class="test-result ok">
            <span>✓</span>{{ sfLoginMessage }}
          </div>
          <div v-if="sfLoginError" class="test-result fail">
            <span>✕</span>{{ sfLoginError }}
          </div>
        </div>

        <div v-if="testResults.salesforce" class="test-result" :class="testResults.salesforce.ok ? 'ok' : 'fail'">
          <span>{{ testResults.salesforce.ok ? '✓' : '✕' }}</span>{{ testResults.salesforce.detail }}
        </div>
      </article>

      <footer class="settings-foot">
        <p>配置文件位置：<code>{{ storagePath }}</code></p>
        <p>凭据仅保存在本机，不随代码提交。密码类字段读取时脱敏，留空即表示保持原值。</p>
      </footer>
    </template>
  </section>
</template>
