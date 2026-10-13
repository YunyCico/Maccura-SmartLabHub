<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api/http'
import LisDataOrganizer from './LisDataOrganizer.vue'

const router = useRouter()
const showLisOrganizer = ref(false)
const url = ref('')
const loading = ref(true)
const error = ref('')
const embedFrame = ref<HTMLIFrameElement | null>(null)

function handleEmbedMessage(event: MessageEvent) {
  if (event.source !== embedFrame.value?.contentWindow) return
  if (!url.value || event.origin !== new URL(url.value, window.location.href).origin) return
  if (event.data?.type === 'smartlabhub:open-lis-organizer') showLisOrganizer.value = true
  if (event.data?.type === 'smartlabhub:navigate') showLisOrganizer.value = false
}

function closeLisOrganizer() {
  showLisOrganizer.value = false
  if (!url.value) return
  embedFrame.value?.contentWindow?.postMessage(
    { type: 'smartlabhub:close-lis-organizer' },
    new URL(url.value, window.location.href).origin,
  )
}

// 回主页前先把 LIS 覆盖层撤掉，避免下次进来还停在 LIS 界面
function backToHome() {
  closeLisOrganizer()
  router.push('/')
}

async function loadEmbed() {
  loading.value = true
  error.value = ''
  try {
    let status = (await api.get('/data-analysis/embed-status')).data
    if (!status.running || !status.url) {
      status = (await api.post('/data-analysis/embed/start')).data
    }
    url.value = status.url
    if (!url.value) error.value = 'SmartLabHub 界面服务未能启动，请重启后端。'
  } catch (embedError: any) {
    error.value = embedError?.response?.data?.detail || '无法连接 SmartLabHub 界面服务。'
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  window.addEventListener('message', handleEmbedMessage)
  loadEmbed()
})
onUnmounted(() => window.removeEventListener('message', handleEmbedMessage))
</script>

<template>
  <section class="embed-shell" :class="{ 'is-lis-open': showLisOrganizer }">
    <div class="embed-toolbar">
      <div>
        <p class="section-kicker">SMARTLABHUB 数据汇总助手 · 原样迁入</p>
        <h2>数据分析工作台</h2>
      </div>
      <div class="embed-actions">
        <button class="primary-button back-home-button" type="button" @click="backToHome">返回 SmartLabHub <span>↗</span></button>
        <a v-if="url" class="secondary-button" :href="url" target="_blank" rel="noopener">原窗口打开</a>
        <button class="secondary-button" type="button" @click="loadEmbed">重新连接 <span>↻</span></button>
      </div>
    </div>
    <p v-if="error" class="form-error">{{ error }}</p>
    <p v-if="loading" class="loading-line">正在启动 SmartLabHub 界面服务…</p>
    <div class="embed-workspace">
      <iframe v-if="url" ref="embedFrame" class="embed-frame" :src="url" title="SmartLabHub 数据分析工作台"></iframe>
      <main v-if="showLisOrganizer" class="lis-workspace" aria-label="LIS 数据整理">
        <div class="lis-workspace-toolbar">
          <button class="ghost-button" type="button" @click="closeLisOrganizer">← 返回数据分析</button>
        </div>
        <LisDataOrganizer />
      </main>
    </div>
  </section>
</template>
