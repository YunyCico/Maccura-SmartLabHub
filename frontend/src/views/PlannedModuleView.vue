<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'

const route = useRoute()
const router = useRouter()

const title = computed(() => (route.meta.title as string) ?? '模块')
const code = computed(() => (route.meta.code as string) ?? '')
const summary = computed(() => (route.meta.summary as string) ?? '')
const points = computed(() => (route.meta.points as string[]) ?? [])
</script>

<template>
  <section class="planned-shell">
    <div class="planned-card">
      <span class="planned-badge">规划中 · MODULE {{ code }}</span>
      <h2>{{ title }}</h2>
      <p class="planned-summary">{{ summary }}</p>
      <ul class="planned-points">
        <li v-for="point in points" :key="point">
          <span class="point-dot" aria-hidden="true"></span>{{ point }}
        </li>
      </ul>
      <div class="planned-note">
        <span class="status-dot"></span>
        该模块入口已保留，功能开发完成后将在此页面上线。
      </div>
      <button class="primary-button" type="button" @click="router.push('/')">返回 SmartLabHub <span>↗</span></button>
    </div>
  </section>
</template>
