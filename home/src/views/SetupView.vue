<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { authState, ensureAuthLoaded, setupAdmin } from '../api/auth'

const router = useRouter()

const username = ref('')
const displayName = ref('')
const password = ref('')
const confirm = ref('')
const showPassword = ref(false)
const busy = ref(false)
const error = ref('')

const passwordTooShort = computed(() => password.value.length > 0 && password.value.length < 8)
const mismatch = computed(() => confirm.value.length > 0 && confirm.value !== password.value)
const canSubmit = computed(
  () =>
    username.value.trim().length >= 3 &&
    password.value.length >= 8 &&
    confirm.value === password.value &&
    !busy.value,
)

async function submit() {
  if (!canSubmit.value) return
  busy.value = true
  error.value = ''
  try {
    await setupAdmin({
      username: username.value.trim(),
      password: password.value,
      display_name: displayName.value.trim(),
    })
    router.replace('/')
  } catch (e: any) {
    error.value = e?.response?.data?.detail || '创建失败，请确认后端服务是否已启动。'
  } finally {
    busy.value = false
  }
}

onMounted(async () => {
  await ensureAuthLoaded(true)
  // 已经初始化过就不该再进这一步
  if (authState.initialized) {
    router.replace(authState.user ? '/' : '/login')
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

      <h1 class="auth-title">首次设置管理员</h1>
      <p class="auth-hint">
        平台还没有任何账号。请先创建第一个管理员账号 —— 建完会自动登录。
        之后你可以用这个账号在「账号管理」里给同事开号。
      </p>

      <form class="auth-form" @submit.prevent="submit">
        <label class="auth-field">
          <span>管理员账号</span>
          <input
            v-model="username"
            type="text"
            autocomplete="username"
            placeholder="至少 3 个字符，不含空格"
            :disabled="busy"
            autofocus
          />
        </label>

        <label class="auth-field">
          <span>显示名称</span>
          <input v-model="displayName" type="text" placeholder="例如 孟鹏杰（可留空）" :disabled="busy" />
        </label>

        <label class="auth-field">
          <span>密码</span>
          <div class="auth-password">
            <input
              v-model="password"
              :type="showPassword ? 'text' : 'password'"
              autocomplete="new-password"
              placeholder="至少 8 位"
              :disabled="busy"
            />
            <button
              type="button"
              class="auth-eye"
              @click="showPassword = !showPassword"
            >{{ showPassword ? '隐藏' : '显示' }}</button>
          </div>
          <small v-if="passwordTooShort" class="auth-warn">密码至少 8 位</small>
        </label>

        <label class="auth-field">
          <span>确认密码</span>
          <input
            v-model="confirm"
            :type="showPassword ? 'text' : 'password'"
            autocomplete="new-password"
            placeholder="再输一次"
            :disabled="busy"
          />
          <small v-if="mismatch" class="auth-warn">两次输入不一致</small>
        </label>

        <p v-if="error" class="auth-error">{{ error }}</p>

        <button class="auth-submit" type="submit" :disabled="!canSubmit">
          {{ busy ? '创建中…' : '创建并登录' }}
        </button>
      </form>

      <p class="auth-foot">
        这个页面只在平台还没有任何账号时出现。创建完成后会被自动关闭。
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
  max-width: 420px;
  padding: 36px 34px 30px;
  background: #fff;
  border: 1px solid rgba(0, 0, 0, .06);
  border-radius: 20px;
  box-shadow: 0 8px 32px rgba(0, 0, 0, .06);
}
.auth-brand { display: flex; align-items: baseline; gap: 10px; margin-bottom: 26px; }
.auth-brand-sub { color: var(--muted); font-size: 12px; }
.auth-title { margin: 0 0 6px; font-size: 22px; font-weight: 600; letter-spacing: -.01em; }
.auth-hint { margin: 0 0 24px; color: var(--muted); font-size: 12.5px; line-height: 1.65; }
.auth-form { display: flex; flex-direction: column; gap: 15px; }
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
.auth-warn { color: #b3261e; font-size: 12px; }
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
.auth-foot { margin: 20px 0 0; color: var(--muted); font-size: 12px; line-height: 1.6; text-align: center; }
</style>
