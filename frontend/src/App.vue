<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { fetchCurrentUser, fetchDingTalkConfig, type CurrentUser } from './api/user'
import { resolveDingTalkAuthCode } from './lib/dingtalk'

const route = useRoute()
const user = ref<CurrentUser | null>(null)

const currentTitle = computed(() => (route.meta.title as string) ?? 'SmartLabHub')
const initials = computed(() => (user.value?.name ? user.value.name.slice(0, 1) : '…'))

async function loadUser() {
  try {
    const config = await fetchDingTalkConfig()
    const code = config.enabled && config.corp_id ? await resolveDingTalkAuthCode(config.corp_id) : null
    user.value = await fetchCurrentUser(code)
  } catch {
    user.value = null
  }
}

onMounted(loadUser)
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
          <span>{{ user?.name ?? '识别中…' }}</span>
          <small>{{ user?.role ?? 'department_admin' }}</small>
        </div>
        <img v-if="user?.avatar" class="avatar avatar-img" :src="user.avatar" :alt="user.name" />
        <div v-else class="avatar">{{ initials }}</div>
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
