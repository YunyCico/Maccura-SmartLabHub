<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { authState, ensureAuthLoaded, isAdmin, logout } from './api/auth'
import DataAnalysisView from './views/DataAnalysisView.vue'

const route = useRoute()
const router = useRouter()

const userMenuOpen = ref(false)

const currentTitle = computed(() => (route.meta.title as string) ?? 'SmartLabHub')
const isDataAnalysis = computed(() => route.path === '/data-analysis')
// 登录页/首次设置页不套外壳（没有顶部用户栏、没有退出入口）
const isBare = computed(() => Boolean(route.meta.bare))
// 内嵌整页模块（如服务需求报表）要铺满，不要工作区的左右留白
const isFullBleed = computed(() => Boolean(route.meta.fullBleed))

const user = computed(() => authState.user)
const initials = computed(() =>
  user.value?.display_name ? user.value.display_name.slice(0, 1) : '…',
)
const roleLabel = computed(() =>
  user.value?.role === 'admin' ? '管理员' : user.value ? '普通成员' : '',
)

async function handleLogout() {
  userMenuOpen.value = false
  await logout()
  router.replace('/login')
}

function goAccount() {
  userMenuOpen.value = false
  router.push('/account')
}

function toggleMenu() {
  userMenuOpen.value = !userMenuOpen.value
}

onMounted(() => {
  void ensureAuthLoaded()
  document.addEventListener('click', (event) => {
    const target = event.target as HTMLElement | null
    if (!target?.closest('.user-block')) userMenuOpen.value = false
  })
})
</script>

<template>
  <!-- 登录页 / 首次设置页：纯页面，不套外壳 -->
  <RouterView v-if="isBare" />

  <div v-else class="app-shell" :class="{ 'shell-full': isFullBleed }">
    <header class="brand-bar">
      <div class="brand-lockup">
        <span class="brand-maccura">maccura</span>
        <span class="brand-subtitle">实验室运营管理平台</span>
      </div>
      <div class="user-block">
        <button
          class="user-trigger"
          type="button"
          :aria-expanded="userMenuOpen"
          aria-haspopup="menu"
          @click.stop="toggleMenu"
        >
          <span class="user-copy">
            <span>{{ user?.display_name ?? '加载中…' }}</span>
            <small>{{ roleLabel || '—' }}</small>
          </span>
          <div class="avatar" :class="{ 'avatar-empty': !user?.avatar }">
            <img v-if="user?.avatar" :src="user.avatar" :alt="user?.display_name || '头像'" />
            <template v-else>{{ initials }}</template>
          </div>
        </button>

        <div v-if="userMenuOpen" class="user-menu" role="menu">
          <div class="user-menu-head">
            <strong>{{ user?.display_name }}</strong>
            <span>{{ user?.username }}</span>
          </div>
          <button type="button" role="menuitem" @click="goAccount">
            {{ isAdmin() ? '账号管理' : '我的账号 / 改密码' }}
          </button>
          <button type="button" role="menuitem" class="danger" @click="handleLogout">退出登录</button>
        </div>
      </div>
    </header>

    <main class="workspace" :class="{ 'workspace-full': isFullBleed }">
      <header v-if="route.path === '/'" class="topbar">
        <h1>{{ currentTitle }}</h1>
      </header>

      <div v-show="isDataAnalysis" class="persist-slot">
        <DataAnalysisView />
      </div>

      <RouterView v-if="!isDataAnalysis" />
    </main>
  </div>
</template>

<style scoped>
.user-block { position: relative; }
.user-trigger {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 4px 6px 4px 14px;
  border: 0;
  border-radius: 999px;
  background: transparent;
  cursor: pointer;
  transition: background .18s;
}
.user-trigger:hover { background: rgba(0, 0, 0, .04); }
.user-menu {
  position: absolute;
  top: calc(100% + 8px);
  right: 0;
  min-width: 200px;
  padding: 8px;
  background: #fff;
  border: 1px solid rgba(0, 0, 0, .08);
  border-radius: 14px;
  box-shadow: 0 12px 32px rgba(0, 0, 0, .12);
  z-index: 40;
}
.user-menu-head {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px 10px 10px;
  margin-bottom: 6px;
  border-bottom: 1px solid rgba(0, 0, 0, .06);
}
.user-menu-head strong { font-size: 13px; font-weight: 500; }
.user-menu-head span { color: var(--muted); font-size: 11.5px; }
.user-menu button {
  display: block;
  width: 100%;
  padding: 8px 10px;
  border: 0;
  border-radius: 9px;
  background: transparent;
  color: var(--ink);
  font: inherit;
  font-size: 13px;
  text-align: left;
  cursor: pointer;
}
.user-menu button:hover { background: #f5f5f7; }
.user-menu button.danger { color: #b3261e; }
.user-menu button.danger:hover { background: #fdecec; }
</style>
