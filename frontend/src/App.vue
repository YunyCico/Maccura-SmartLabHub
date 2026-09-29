<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()

const currentTitle = computed(() => (route.meta.title as string) ?? '工作台')
</script>

<template>
  <div class="app-shell">
    <header class="brand-bar">
      <div class="brand-lockup">
        <span class="brand-maccura">maccura</span>
        <span class="brand-subtitle">实验室运营管理平台</span>
      </div>
      <div class="user-block">
        <div class="user-copy">
          <span>本地管理员</span>
          <small>department_admin</small>
        </div>
        <div class="avatar">管</div>
      </div>
    </header>

    <main class="workspace">
      <header class="topbar">
        <div>
          <p class="eyebrow">OPERATIONS CONTROL ROOM / 2026.09</p>
          <h1>{{ currentTitle }}</h1>
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
