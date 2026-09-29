<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()

const navigation = [
  { label: '工作台', code: '01', to: '/' },
  { label: '数据分析', code: '02', to: '/data-analysis' },
  { label: '钉钉听记内容标准化处理', code: '03', to: '/dingtalk-minutes' },
  { label: '6S标准化报告', code: '04', to: '/6s-report' },
]

const currentTitle = computed(() => (route.meta.title as string) ?? '工作台')
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand-lockup">
        <span class="brand-maccura">maccura</span>
        <span class="brand-subtitle">实验室运营管理平台</span>
      </div>

      <div class="sidebar-rule"></div>
      <p class="nav-caption">部门工作区</p>
      <nav class="main-nav" aria-label="主导航">
        <RouterLink
          v-for="item in navigation"
          :key="item.to"
          class="nav-item"
          :to="item.to"
          :class="{ active: route.path === item.to }"
        >
          <span class="nav-code">{{ item.code }}</span>
          <span>{{ item.label }}</span>
          <span v-if="route.path === item.to" class="nav-arrow" aria-hidden="true">↗</span>
        </RouterLink>
      </nav>

      <div class="sidebar-bottom">
        <div class="status-line"><span class="status-dot"></span>本地开发环境</div>
        <p>钉钉挂载版本 · 预备中</p>
      </div>
    </aside>

    <main class="workspace">
      <header class="topbar">
        <div>
          <p class="eyebrow">OPERATIONS CONTROL ROOM / 2026.09</p>
          <h1>{{ currentTitle }}</h1>
        </div>
        <div class="user-block">
          <div class="user-copy">
            <span>本地管理员</span>
            <small>department_admin</small>
          </div>
          <div class="avatar">管</div>
        </div>
      </header>

      <RouterView v-slot="{ Component }">
        <transition name="page-fade" mode="out-in">
          <component :is="Component" />
        </transition>
      </RouterView>
    </main>
  </div>
</template>
