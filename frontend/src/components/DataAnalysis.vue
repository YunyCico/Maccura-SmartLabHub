<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api/http'

const router = useRouter()
const url = ref('')
const loading = ref(true)
const error = ref('')

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

onMounted(loadEmbed)
</script>

<template>
  <section class="embed-shell">
    <div class="embed-toolbar">
      <div>
        <p class="section-kicker">SMARTLABHUB 数据汇总助手 · 原样迁入</p>
        <h2>数据分析工作台</h2>
      </div>
      <div class="embed-actions">
        <button class="ghost-button" type="button" @click="router.push('/')">← 工作台</button>
        <a v-if="url" class="secondary-button" :href="url" target="_blank" rel="noopener">原窗口打开</a>
        <button class="primary-button" type="button" @click="loadEmbed">重新连接 <span>↻</span></button>
      </div>
    </div>
    <p v-if="error" class="form-error">{{ error }}</p>
    <p v-if="loading" class="loading-line">正在启动 SmartLabHub 界面服务…</p>
    <iframe v-if="url" class="embed-frame" :src="url" title="SmartLabHub 数据分析工作台"></iframe>
  </section>
</template>
