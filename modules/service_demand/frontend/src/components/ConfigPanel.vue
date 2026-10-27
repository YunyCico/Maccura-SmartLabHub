<script setup>
import { ref } from 'vue'
import { api } from '../api.js'

const props = defineProps({ conn: Object, username: String, apiBase: String })
const emit = defineEmits(['saved'])

const open = ref(false)
const saving = ref(false)
const msg = ref('')
const err = ref('')
const form = ref({ username: '', openApiKey: '' })

async function load() {
  try {
    const c = await api.getConfig()
    form.value.username = c.username || ''
  } catch (e) { err.value = e.message }
}

async function toggle() {
  open.value = !open.value
  if (open.value) { msg.value = ''; err.value = ''; await load() }
}

async function save() {
  saving.value = true; err.value = ''; msg.value = ''
  try {
    const patch = { username: form.value.username }
    if (form.value.openApiKey) patch.openApiKey = form.value.openApiKey
    await api.saveConfig(patch)
    if (form.value.openApiKey) form.value.openApiKey = ''
    msg.value = '配置已保存'
    emit('saved')
    setTimeout(() => { open.value = false }, 800)
  } catch (e) { err.value = e.message } finally { saving.value = false }
}
</script>

<template>
  <div class="panel">
    <div class="panel-head">
      <h2>必要配置</h2>
      <div class="topbar-spacer"></div>
      <button class="btn" style="padding:4px 11px;font-size:11.5px" @click="toggle">
        {{ open ? '收起' : '修改' }}
      </button>
    </div>
    <div class="panel-body">
      <div class="config-intro">
        <strong>首次使用先完成连接</strong>
        <span>用户名和授权 Key 由数据中台管理员提供，一般只需填写一次。</span>
      </div>
      <div class="field">
        <label>数据权限用户名<span class="req">*</span></label>
        <input type="text" :value="form.username" @input="form.username = $event.target.value"
               placeholder="例如：10399" />
        <div class="hint">这是你在数据中台的工号或用户名，不是登录密码。</div>
      </div>

      <div v-if="open" class="field">
        <label>授权 Key<span class="req">*</span></label>
        <input type="password" v-model="form.openApiKey"
               :placeholder="form.openApiKey ? '' : '粘贴管理员发给你的授权 Key'" />
        <div class="hint">这是管理员分发的接口授权凭证，不是普通登录密码。保存后页面只显示掩码。</div>
      </div>

      <div class="kv">
        <span class="kv-k">数据中台</span>
        <span class="kv-v">{{ props.apiBase }}</span>
      </div>
      <div class="kv">
        <span class="kv-k">连接方式</span>
        <span class="kv-v">系统自动完成，无需手动登录</span>
      </div>
      <div v-if="conn.detail" class="kv">
        <span class="kv-k">连接状态</span>
        <span class="kv-v">{{ conn.detail }}</span>
      </div>

      <div v-if="msg" class="alert alert-info">{{ msg }}</div>
      <div v-if="err" class="alert alert-err">{{ err }}</div>
      <button v-if="open" class="btn btn-primary btn-block" @click="save" :disabled="saving">
        <span v-if="saving" class="spin"></span> 保存并检测连接
      </button>
    </div>
  </div>
</template>