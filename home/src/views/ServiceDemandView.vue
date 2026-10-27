<script setup lang="ts">
/**
 * 服务需求报表（原 DataFetchPortal）。
 *
 * 用 iframe 内嵌而不是把它的页面搬进本工程：
 *   - 它是一套完整的 Vue 应用（自己的路由、自己的样式），拆开重写成本高、也没必要
 *   - 后端已经把它挂在 /portal，登录守卫自动覆盖它的接口
 *   - 同源，出问题时能直接读到 contentDocument 排查
 *
 * 地址用相对路径 /portal/，所以不管是本机、局域网 IP 还是域名访问都能用。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

const router = useRouter()
const frame = ref<HTMLIFrameElement | null>(null)
const loading = ref(true)
const error = ref('')

const src = computed(() => '/portal/')

function onLoad() {
  loading.value = false
  // 同源才能读 contentDocument；读不到（跨域/报错页）就提示一下
  try {
    const doc = frame.value?.contentDocument
    if (doc && doc.body && doc.body.innerText.trim().length < 10) {
      error.value = '报表页面加载为空，请确认后端已重启。'
    }
  } catch {
    error.value = '报表页面无法读取，请刷新重试。'
  }
}

function reload() {
  loading.value = true
  error.value = ''
  // 直接改 src 才真正重新拉取（只改 ref 不会重载 iframe）
  if (frame.value) frame.value.src = src.value
}

onMounted(() => {
  // 兜底：8 秒还没 load 事件就当超时提示，避免一直转圈
  window.setTimeout(() => {
    if (loading.value) {
      loading.value = false
      error.value = '报表页面加载超时，请点「重新加载」或重启平台。'
    }
  }, 8000)
})
</script>

<template>
  <section class="report-page">
    <header class="report-head">
      <div>
        <p class="report-kicker">部门工作模块</p>
        <h1>服务需求报表</h1>
        <p class="report-sub">按条件从数据中台拉取服务需求明细，自动生成 KPI 看板与分析图表。</p>
      </div>
      <div class="report-actions">
        <button type="button" @click="router.push('/')">← 回工作台</button>
        <button type="button" @click="reload">重新加载</button>
      </div>
    </header>

    <p v-if="error" class="report-banner">{{ error }}</p>

    <div class="report-frame-wrap">
      <div v-if="loading" class="report-loading">报表加载中…</div>
      <iframe
        ref="frame"
        class="report-frame"
        :src="src"
        title="服务需求报表"
        @load="onLoad"
      ></iframe>
    </div>
  </section>
</template>

<style scoped>
.report-page {
  display: flex;
  flex-direction: column;
  height: calc(100vh - clamp(60px, 3vw, 84px));
  padding: 20px clamp(16px, 2vw, 36px) 24px;
}
.report-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  gap: 16px;
  padding-bottom: 14px;
}
.report-kicker { margin: 0 0 3px; color: var(--muted); font-size: 12px; }
.report-head h1 { margin: 0 0 4px; font-size: 22px; font-weight: 600; letter-spacing: -.01em; }
.report-sub { margin: 0; color: var(--muted); font-size: 12.5px; }
.report-actions { display: flex; gap: 8px; flex: 0 0 auto; }
.report-actions button {
  padding: 7px 16px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: #fff;
  color: var(--ink);
  font-size: 12.5px;
  cursor: pointer;
  transition: border-color .18s, color .18s;
}
.report-actions button:hover { border-color: var(--blue); color: var(--blue); }
.report-banner {
  margin: 0 0 10px;
  padding: 9px 14px;
  border-radius: 10px;
  background: #fdecec;
  color: #b3261e;
  font-size: 12.5px;
}
.report-frame-wrap {
  position: relative;
  flex: 1;
  min-height: 0;
  border: 1px solid rgba(0, 0, 0, .07);
  border-radius: 16px;
  background: #fff;
  overflow: hidden;
}
.report-frame {
  width: 100%;
  height: 100%;
  border: 0;
  display: block;
}
.report-loading {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--muted);
  font-size: 13px;
  background: #fff;
  z-index: 1;
}
</style>
