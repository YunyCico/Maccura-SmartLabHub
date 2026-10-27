<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  authState,
  changeOwnPassword,
  createUser,
  deleteUser,
  isAdmin,
  listUsers,
  resetUserPassword,
  updateOwnProfile,
  updateUser,
  type AuthUser,
} from '../api/auth'
import UserAvatar from '../components/UserAvatar.vue'

const router = useRouter()

const users = ref<AuthUser[]>([])
const loading = ref(true)
const busy = ref(false)
const error = ref('')
const notice = ref('')

// 我的资料（名称 + 头像）
const me = ref({ display_name: '', avatar: '' })
const meBusy = ref(false)
const meError = ref('')
const meNotice = ref('')

// 新建账号表单
const showCreate = ref(false)
const form = ref({
  username: '',
  display_name: '',
  password: '',
  role: 'member' as 'admin' | 'member',
  avatar: '',
})

// 管理员编辑他人：正在编辑的行 + 草稿
const editingId = ref<number | null>(null)
const draft = ref({ display_name: '', avatar: '' })

// 改自己的密码
const pwd = ref({ old: '', next: '', confirm: '' })
const pwdError = ref('')
const pwdNotice = ref('')

const isAdminUser = computed(() => isAdmin())
const canCreate = computed(
  () =>
    form.value.username.trim().length >= 2 &&
    form.value.password.length >= 8 &&
    !busy.value,
)

function syncMe() {
  const u = authState.user
  if (u) me.value = { display_name: u.display_name, avatar: u.avatar || '' }
}

async function saveMe() {
  meBusy.value = true
  meError.value = ''
  meNotice.value = ''
  try {
    await updateOwnProfile({
      display_name: me.value.display_name.trim(),
      avatar: me.value.avatar,
    })
    meNotice.value = '资料已保存'
    syncMe()
    await load()
  } catch (e: any) {
    meError.value = e?.response?.data?.detail || '保存失败'
  } finally {
    meBusy.value = false
  }
}

async function load() {
  if (!isAdminUser.value) {
    loading.value = false
    return
  }
  loading.value = true
  error.value = ''
  try {
    users.value = await listUsers()
  } catch (e: any) {
    error.value = e?.response?.data?.detail || '账号列表加载失败'
  } finally {
    loading.value = false
  }
}

async function submitCreate() {
  if (!canCreate.value) return
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    const created = await createUser({
      username: form.value.username.trim(),
      display_name: form.value.display_name.trim(),
      password: form.value.password,
      role: form.value.role,
      avatar: form.value.avatar,
    })
    notice.value = `已创建账号「${created.username}」，请把密码告诉对方（只显示这一次）。`
    form.value = { username: '', display_name: '', password: '', role: 'member', avatar: '' }
    showCreate.value = false
    await load()
  } catch (e: any) {
    error.value = e?.response?.data?.detail || '创建失败'
  } finally {
    busy.value = false
  }
}

function startEdit(user: AuthUser) {
  editingId.value = user.id
  draft.value = { display_name: user.display_name, avatar: user.avatar || '' }
  error.value = ''
  notice.value = ''
}

function cancelEdit() {
  editingId.value = null
}

async function saveEdit(user: AuthUser) {
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    await updateUser(user.id, {
      display_name: draft.value.display_name.trim(),
      avatar: draft.value.avatar,
    })
    notice.value = `「${user.username}」的资料已更新`
    editingId.value = null
    await load()
  } catch (e: any) {
    error.value = e?.response?.data?.detail || '保存失败'
  } finally {
    busy.value = false
  }
}

async function toggleRole(user: AuthUser) {
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    const next = user.role === 'admin' ? 'member' : 'admin'
    await updateUser(user.id, { role: next })
    notice.value = `「${user.username}」已改为${next === 'admin' ? '管理员' : '普通成员'}`
    await load()
  } catch (e: any) {
    error.value = e?.response?.data?.detail || '修改失败'
  } finally {
    busy.value = false
  }
}

async function toggleActive(user: AuthUser) {
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    await updateUser(user.id, { active: !user.active })
    notice.value = `「${user.username}」已${user.active ? '停用' : '启用'}`
    await load()
  } catch (e: any) {
    error.value = e?.response?.data?.detail || '修改失败'
  } finally {
    busy.value = false
  }
}

async function askReset(user: AuthUser) {
  const next = window.prompt(`给「${user.username}」设置新密码（至少 8 位）：`)
  if (!next) return
  if (next.length < 8) {
    error.value = '密码至少 8 位'
    return
  }
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    const detail = await resetUserPassword(user.id, next)
    notice.value = `「${user.username}」${detail}`
    await load()
  } catch (e: any) {
    error.value = e?.response?.data?.detail || '重置失败'
  } finally {
    busy.value = false
  }
}

async function askDelete(user: AuthUser) {
  if (!window.confirm(`确定删除账号「${user.username}」吗？\n\n删除后该账号立即失效，且不可恢复。`)) return
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    await deleteUser(user.id)
    notice.value = `已删除「${user.username}」`
    await load()
  } catch (e: any) {
    error.value = e?.response?.data?.detail || '删除失败'
  } finally {
    busy.value = false
  }
}

async function submitPassword() {
  pwdError.value = ''
  pwdNotice.value = ''
  if (pwd.value.next.length < 8) {
    pwdError.value = '新密码至少 8 位'
    return
  }
  if (pwd.value.next !== pwd.value.confirm) {
    pwdError.value = '两次输入的新密码不一致'
    return
  }
  busy.value = true
  try {
    const detail = await changeOwnPassword(pwd.value.old, pwd.value.next)
    pwdNotice.value = detail
    pwd.value = { old: '', next: '', confirm: '' }
    // 会话已被后端清掉，回登录页
    setTimeout(() => router.replace('/login'), 1200)
  } catch (e: any) {
    pwdError.value = e?.response?.data?.detail || '修改失败'
  } finally {
    busy.value = false
  }
}

function formatDate(iso: string): string {
  if (!iso) return '—'
  return iso.replace('T', ' ').slice(0, 16)
}

onMounted(async () => {
  syncMe()
  await load()
})
</script>

<template>
  <section class="account-page">
    <header class="account-head">
      <div>
        <p class="account-kicker">账号与权限</p>
        <h1>账号管理</h1>
      </div>
      <button v-if="isAdminUser" class="account-primary" type="button" @click="showCreate = !showCreate">
        {{ showCreate ? '取消' : '新建账号' }}
      </button>
    </header>

    <p v-if="error" class="account-banner error">{{ error }}</p>
    <p v-if="notice" class="account-banner ok">{{ notice }}</p>

    <!-- 我的资料：名称 + 头像，所有人都能改 -->
    <div class="account-card">
      <h2>我的资料</h2>
      <div class="profile-row">
        <UserAvatar
          :src="me.avatar"
          :name="me.display_name"
          :size="76"
          editable
          @update:src="me.avatar = $event"
          @error="meError = $event"
        />
        <div class="profile-fields">
          <label class="account-field">
            <span>显示名称</span>
            <input
              v-model="me.display_name"
              type="text"
              placeholder="比如 孟鹏杰"
              maxlength="24"
            />
          </label>
          <p class="account-tip profile-hint">
            登录账号：<strong>{{ authState.user?.username }}</strong>
            （账号不能改，需要换请联系管理员）
          </p>
        </div>
      </div>
      <p v-if="meError" class="account-banner error">{{ meError }}</p>
      <p v-if="meNotice" class="account-banner ok">{{ meNotice }}</p>
      <button class="account-primary" type="button" :disabled="meBusy" @click="saveMe">
        {{ meBusy ? '保存中…' : '保存资料' }}
      </button>
    </div>

    <!-- 新建账号 -->
    <div v-if="showCreate && isAdminUser" class="account-card">
      <h2>新建账号</h2>
      <div class="profile-row">
        <UserAvatar
          :src="form.avatar"
          :name="form.display_name || form.username"
          :size="68"
          editable
          @update:src="form.avatar = $event"
          @error="error = $event"
        />
        <div class="profile-fields">
          <div class="account-grid">
            <label class="account-field">
              <span>登录账号（工号）</span>
              <input v-model="form.username" type="text" placeholder="比如 95 或 8873" />
            </label>
            <label class="account-field">
              <span>显示名称</span>
              <input v-model="form.display_name" type="text" placeholder="比如 孟鹏杰（可留空）" />
            </label>
            <label class="account-field">
              <span>初始密码</span>
              <input v-model="form.password" type="text" placeholder="至少 8 位" />
            </label>
            <label class="account-field">
              <span>角色</span>
              <select v-model="form.role">
                <option value="member">普通成员（只能用业务功能）</option>
                <option value="admin">管理员（可管账号与集成配置）</option>
              </select>
            </label>
          </div>
        </div>
      </div>
      <p class="account-tip">
        初始密码会明文显示在输入框里 —— 建完把密码告诉对方，让他登录后自己改。
      </p>
      <button class="account-primary" type="button" :disabled="!canCreate" @click="submitCreate">
        {{ busy ? '创建中…' : '创建账号' }}
      </button>
    </div>

    <!-- 账号列表 -->
    <div v-if="isAdminUser" class="account-card">
      <h2>已有账号 <span class="account-count">{{ users.length }}</span></h2>
      <div v-if="loading" class="account-empty">加载中…</div>
      <div v-else-if="!users.length" class="account-empty">还没有账号。</div>
      <table v-else class="account-table">
        <thead>
          <tr>
            <th>账号</th>
            <th>显示名称</th>
            <th>角色</th>
            <th>状态</th>
            <th>最近登录</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="u in users" :key="u.id" :class="{ off: !u.active }">
            <td class="mono">{{ u.username }}</td>
            <td>
              <div class="cell-person">
                <UserAvatar :src="u.avatar" :name="u.display_name" :size="30" />
                <span>{{ u.display_name }}</span>
              </div>
            </td>
            <td>
              <span class="account-tag" :class="u.role">{{ u.role === 'admin' ? '管理员' : '普通成员' }}</span>
            </td>
            <td>
              <span class="account-tag" :class="u.active ? 'on' : 'off'">{{ u.active ? '正常' : '已停用' }}</span>
            </td>
            <td class="mono">{{ formatDate(u.last_login_at) }}</td>
            <td class="account-actions">
              <button type="button" @click="editingId === u.id ? cancelEdit() : startEdit(u)" :disabled="busy">
                {{ editingId === u.id ? '收起' : '编辑资料' }}
              </button>
              <button type="button" @click="toggleRole(u)" :disabled="busy">
                {{ u.role === 'admin' ? '降为成员' : '设为管理员' }}
              </button>
              <button type="button" @click="askReset(u)" :disabled="busy">重置密码</button>
              <button type="button" @click="toggleActive(u)" :disabled="busy">
                {{ u.active ? '停用' : '启用' }}
              </button>
              <button type="button" class="danger" @click="askDelete(u)" :disabled="busy">删除</button>
            </td>
          </tr>
          <!-- 编辑行：改别人的名称和头像 -->
          <tr v-if="editingId !== null" class="editor-row">
            <td colspan="6">
              <div class="editor-body">
                <UserAvatar
                  :src="draft.avatar"
                  :name="draft.display_name"
                  :size="64"
                  editable
                  @update:src="draft.avatar = $event"
                  @error="error = $event"
                />
                <label class="account-field editor-name">
                  <span>显示名称</span>
                  <input v-model="draft.display_name" type="text" maxlength="24" placeholder="比如 孟鹏杰" />
                </label>
                <div class="editor-actions">
                  <button class="account-primary" type="button" :disabled="busy" @click="saveEdit(users.find((x) => x.id === editingId)!)">
                    {{ busy ? '保存中…' : '保存' }}
                  </button>
                  <button class="account-ghost" type="button" :disabled="busy" @click="cancelEdit">取消</button>
                </div>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-else class="account-card">
      <h2>权限说明</h2>
      <p class="account-tip">
        你的账号是「普通成员」，可以使用数据分析、LIS 整理、看板这些业务功能；
        账号管理和消息互通配置由管理员负责。
      </p>
    </div>

    <!-- 改自己的密码 -->
    <div class="account-card">
      <h2>修改我的密码</h2>
      <p class="account-tip">当前登录：{{ authState.user?.display_name }}（{{ authState.user?.username }}）。改完需要重新登录。</p>
      <div class="account-grid">
        <label class="account-field">
          <span>当前密码</span>
          <input v-model="pwd.old" type="password" autocomplete="current-password" />
        </label>
        <label class="account-field">
          <span>新密码</span>
          <input v-model="pwd.next" type="password" autocomplete="new-password" placeholder="至少 8 位" />
        </label>
        <label class="account-field">
          <span>确认新密码</span>
          <input v-model="pwd.confirm" type="password" autocomplete="new-password" />
        </label>
      </div>
      <p v-if="pwdError" class="account-banner error">{{ pwdError }}</p>
      <p v-if="pwdNotice" class="account-banner ok">{{ pwdNotice }}</p>
      <button class="account-primary" type="button" :disabled="busy" @click="submitPassword">
        {{ busy ? '提交中…' : '修改密码' }}
      </button>
    </div>
  </section>
</template>

<style scoped>
.account-page { max-width: 1080px; margin: 0 auto; padding: 28px clamp(18px, 2vw, 40px) 60px; }
.account-head { display: flex; justify-content: space-between; align-items: flex-end; gap: 16px; margin-bottom: 20px; }
.account-kicker { margin: 0 0 4px; color: var(--muted); font-size: 12px; }
.account-head h1 { margin: 0; font-size: 24px; font-weight: 600; letter-spacing: -.01em; }
.account-primary {
  padding: 9px 20px; border: 0; border-radius: 999px; background: var(--blue);
  color: #fff; font-size: 13px; font-weight: 500; cursor: pointer; transition: background .18s;
}
.account-primary:hover:not(:disabled) { background: var(--blue-hover); }
.account-primary:disabled { background: #c7c7cc; cursor: not-allowed; }
.account-banner { margin: 0 0 14px; padding: 10px 14px; border-radius: 10px; font-size: 13px; line-height: 1.5; }
.account-banner.error { background: #fdecec; color: #b3261e; }
.account-banner.ok { background: #e8f5e9; color: #1b5e20; }
.account-card {
  margin-bottom: 18px; padding: 22px 24px; background: #fff;
  border: 1px solid rgba(0, 0, 0, .06); border-radius: 16px;
}
.account-card h2 { margin: 0 0 16px; font-size: 15px; font-weight: 600; }
.account-count { margin-left: 6px; color: var(--muted); font-size: 12px; font-weight: 400; }
.account-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 14px; margin-bottom: 14px; }
.account-field { display: flex; flex-direction: column; gap: 6px; }
.account-field > span { color: var(--muted); font-size: 12px; }
.account-field input, .account-field select {
  padding: 10px 12px; border: 1px solid rgba(210, 210, 215, .95); border-radius: 10px;
  background: #fff; color: var(--ink); font: inherit; font-size: 13px;
}
.account-field input:focus, .account-field select:focus {
  outline: none; border-color: var(--blue); box-shadow: 0 0 0 3px rgba(0, 113, 227, .1);
}
.account-tip { margin: 0 0 14px; color: var(--muted); font-size: 12.5px; line-height: 1.65; }
.profile-row { display: flex; gap: 22px; align-items: flex-start; margin-bottom: 14px; }
.profile-fields { flex: 1; min-width: 0; }
.profile-hint { margin: 8px 0 0; }
.cell-person { display: flex; align-items: center; gap: 9px; }
.editor-row td { padding: 0; border-bottom: 1px solid rgba(0, 0, 0, .06); background: #fafafa; }
.editor-body { display: flex; align-items: center; gap: 18px; padding: 16px 12px; }
.editor-name { flex: 0 0 220px; }
.editor-actions { display: flex; gap: 8px; }
.account-ghost {
  padding: 9px 18px; border: 1px solid var(--line); border-radius: 999px;
  background: #fff; color: var(--ink); font-size: 13px; cursor: pointer;
}
.account-ghost:hover { border-color: var(--blue); color: var(--blue); }
.account-empty { padding: 20px 0; color: var(--muted); font-size: 13px; }
.account-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.account-table th, .account-table td { padding: 10px 10px; text-align: left; border-bottom: 1px solid rgba(0, 0, 0, .06); white-space: nowrap; }
.account-table th { color: var(--muted); font-size: 12px; font-weight: 500; }
.account-table tr.off td { color: #a1a1a6; }
.account-table .mono { font-variant-numeric: tabular-nums; }
.account-tag { display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: 11.5px; }
.account-tag.admin { background: rgba(0, 113, 227, .1); color: #0b5cad; }
.account-tag.member { background: #f0f0f2; color: #55555c; }
.account-tag.on { background: #e8f5e9; color: #1b5e20; }
.account-tag.off { background: #fdecec; color: #b3261e; }
.account-actions { display: flex; gap: 6px; flex-wrap: wrap; }
.account-actions button {
  padding: 4px 10px; border: 1px solid var(--line); border-radius: 8px;
  background: #fff; color: var(--ink); font-size: 12px; cursor: pointer;
}
.account-actions button:hover:not(:disabled) { border-color: var(--blue); color: var(--blue); }
.account-actions button.danger:hover:not(:disabled) { border-color: #b3261e; color: #b3261e; }
.account-actions button:disabled { opacity: .5; cursor: not-allowed; }
@media (max-width: 760px) {
  .account-table { display: block; overflow-x: auto; }
}
</style>
