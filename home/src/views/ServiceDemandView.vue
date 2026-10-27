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
  error.value = ''
  // 同源才能读 contentDocument；读不到（跨域/报错页）就提示一下
  try {
    const doc = frame.value?.contentDocument
    if (!doc || !doc.body) return
    const text = doc.body.innerText.trim()
    if (text.length < 10) {
      error.value = '报表页面加载为空，请确认后端已重启。'
      return
    }
    // ★ 防"套娃"：如果 iframe 里装的是平台自己（比如开发服务器没代理 /portal，
    //   SPA 兜底把 index.html 返回来了），页面上会出现平台自己的顶栏。
    //   这种情况必须明确报错，否则只是看起来"有点怪"，很难定位。
    const looksLikePlatform =
      Boolean(doc.querySelector('.app-shell, .brand-bar')) &&
      !/数据中台|DataFetch/.test(doc.title)
    if (looksLikePlatform) {
      error.value =
        '报表模块没有正确加载（加载到的还是平台页面）。多半是后端没重启，或者开发服务器没代理 /portal。'
    }
  } catch {
    error.value = '报表页面无法读取，请刷新重试。'
  }
}

function goHome() {
  router.push('/')
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
    <!-- 不再自建标题栏：报表自己顶部就有「数据中台 / 数据拉取 / BI 看板」，
         再叠一层平台标题只会让顶部占掉两行。回工作台/重新加载已并入报表工具栏。 -->
    <p v-if="error" class="report-banner">
      <span>{{ error }}</span>
      <span class="report-banner-actions">
        <button type="button" @click="reload">重新加载</button>
        <!-- 报表没加载出来时，它工具栏里的「回工作台」也就不存在了，这里兜一个 -->
        <button type="button" @click="goHome">← 回工作台</button>
      </span>
    </p>

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
  /* 铺满：外层的边距/圆角边框由 .workspace-full 和下面的 wrap 一起取消 */
  height: calc(100vh - clamp(60px, 3vw, 84px));
}
.report-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin: 10px 14px;
  padding: 9px 14px;
  border-radius: 10px;
  background: #fdecec;
  color: #b3261e;
  font-size: 12.5px;
}
.report-banner-actions { display: flex; gap: 6px; flex: 0 0 auto; }
.report-banner button {
  flex: 0 0 auto;
  padding: 4px 12px;
  border: 1px solid rgba(179, 38, 30, .35);
  border-radius: 999px;
  background: #fff;
  color: #b3261e;
  font-size: 12px;
  cursor: pointer;
}
.report-frame-wrap {
  position: relative;
  flex: 1;
  min-height: 0;
  /* 不要边框和圆角：四周留框会在报表外面多出一圈灰边 */
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
