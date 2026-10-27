<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ensureAuthLoaded, login, authState } from '../api/auth'

const router = useRouter()
const route = useRoute()

const username = ref('')
const password = ref('')
const showPassword = ref(false)
const busy = ref(false)
const error = ref('')

const canSubmit = computed(
  () => username.value.trim().length > 0 && password.value.length > 0 && !busy.value,
)

function safeRedirect(): string {
  // 只接受站内相对路径，防止 ?redirect=//evil.com 这类开放重定向
  const raw = String(route.query.redirect || '')
  if (raw.startsWith('/') && !raw.startsWith('//')) return raw
  return '/'
}

async function submit() {
  if (!canSubmit.value) return
  busy.value = true
  error.value = ''
  try {
    await login(username.value.trim(), password.value)
    router.replace(safeRedirect())
  } catch (e: any) {
    error.value = e?.response?.data?.detail || '登录失败，请确认后端服务是否已启动。'
    password.value = ''
  } finally {
    busy.value = false
  }
}

onMounted(async () => {
  await ensureAuthLoaded(true)
  // 还没建过账号 -> 直接去首次设置
  if (!authState.initialized) {
    router.replace('/setup')
    return
  }
  // 已经登录就不用再登一次
  if (authState.user) {
    router.replace(safeRedirect())
  }
})
</script>

<template>
  <div class="auth-page">
    <div class="auth-card">
      <div class="auth-brand">
        <span class="brand-maccura">maccura</span>
        <span class="auth-brand-sub">实验室运营管理平台</span>
      </div>

      <h1 class="auth-title">登录</h1>
      <p class="auth-hint">请使用管理员分配的账号登录。忘记密码请找管理员重置。</p>

      <form class="auth-form" @submit.prevent="submit">
        <label class="auth-field">
          <span>账号</span>
          <input
            v-model="username"
            type="text"
            autocomplete="username"
            placeholder="例如 yun"
            :disabled="busy"
            autofocus
          />
        </label>

        <label class="auth-field">
          <span>密码</span>
          <div class="auth-password">
            <input
              v-model="password"
              :type="showPassword ? 'text' : 'password'"
              autocomplete="current-password"
              placeholder="请输入密码"
              :disabled="busy"
            />
            <button
              type="button"
              class="auth-eye"
              :aria-label="showPassword ? '隐藏密码' : '显示密码'"
              @click="showPassword = !showPassword"
            >
              {{ showPassword ? '隐藏' : '显示' }}
            </button>
          </div>
        </label>

        <p v-if="error" class="auth-error">{{ error }}</p>

        <button class="auth-submit" type="submit" :disabled="!canSubmit">
          {{ busy ? '登录中…' : '登录' }}
        </button>
      </form>

      <p class="auth-foot">
        还没有账号？请联系部门管理员在「账号管理」里为你创建。
      </p>
    </div>
  </div>
</template>

<style scoped>
.auth-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 32px 20px;
  background: var(--bg);
}
.auth-card {
  width: 100%;
  max-width: 400px;
  padding: 36px 34px 30px;
  background: #fff;
  border: 1px solid rgba(0, 0, 0, .06);
  border-radius: 20px;
  box-shadow: 0 8px 32px rgba(0, 0, 0, .06);
}
.auth-brand { display: flex; align-items: baseline; gap: 10px; margin-bottom: 26px; }
.auth-brand-sub { color: var(--muted); font-size: 12px; }
.auth-title { margin: 0 0 6px; font-size: 24px; font-weight: 600; letter-spacing: -.01em; }
.auth-hint { margin: 0 0 24px; color: var(--muted); font-size: 12.5px; line-height: 1.6; }
.auth-form { display: flex; flex-direction: column; gap: 16px; }
.auth-field { display: flex; flex-direction: column; gap: 7px; }
.auth-field > span { font-size: 12.5px; color: var(--muted); }
.auth-field input {
  width: 100%;
  padding: 11px 14px;
  border: 1px solid rgba(210, 210, 215, .95);
  border-radius: 10px;
  background: #fff;
  color: var(--ink);
  font: inherit;
  font-size: 14px;
  transition: border-color .2s ease, box-shadow .2s ease;
}
.auth-field input:focus {
  outline: none;
  border-color: var(--blue);
  box-shadow: 0 0 0 3px rgba(0, 113, 227, .1);
}
.auth-field input:disabled { background: #f5f5f7; color: var(--muted); }
.auth-password { position: relative; display: flex; }
.auth-password input { padding-right: 56px; }
.auth-eye {
  position: absolute;
  right: 6px;
  top: 50%;
  transform: translateY(-50%);
  padding: 5px 8px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: var(--blue);
  font-size: 12px;
  cursor: pointer;
}
.auth-eye:hover { background: rgba(0, 113, 227, .08); }
.auth-error {
  margin: 0;
  padding: 9px 12px;
  border-radius: 9px;
  background: #fdecec;
  color: #b3261e;
  font-size: 12.5px;
  line-height: 1.5;
}
.auth-submit {
  margin-top: 4px;
  padding: 12px 18px;
  border: 0;
  border-radius: 999px;
  background: var(--blue);
  color: #fff;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  transition: background .18s;
}
.auth-submit:hover:not(:disabled) { background: var(--blue-hover); }
.auth-submit:disabled { background: #c7c7cc; cursor: not-allowed; }
.auth-foot { margin: 22px 0 0; color: var(--muted); font-size: 12px; line-height: 1.6; text-align: center; }
</style>
