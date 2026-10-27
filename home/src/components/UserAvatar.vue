<script setup lang="ts">
/**
 * 头像：显示 + 可选的上传/清除。
 *
 * 上传的图会先在浏览器里压成 128×128 的 webp 再交给后端，
 * 所以这里只管把 File 变成 data URL，不关心存储。
 */
import { computed, ref } from 'vue'
import { fileToAvatar } from '../lib/avatar'

const props = withDefaults(
  defineProps<{
    src?: string
    name?: string
    size?: number
    editable?: boolean
  }>(),
  { src: '', name: '', size: 64, editable: false },
)

const emit = defineEmits<{
  (event: 'update:src', value: string): void
  (event: 'error', message: string): void
}>()

const input = ref<HTMLInputElement | null>(null)
const busy = ref(false)

const initial = computed(() => (props.name ? props.name.trim().slice(0, 1) : '?'))

function pick() {
  input.value?.click()
}

async function onChange(event: Event) {
  const target = event.target as HTMLInputElement
  const file = target.files?.[0]
  target.value = '' // 允许重复选同一张
  if (!file) return
  busy.value = true
  try {
    emit('update:src', await fileToAvatar(file))
  } catch (error: any) {
    emit('error', error?.message || '图片处理失败')
  } finally {
    busy.value = false
  }
}

function clear() {
  emit('update:src', '')
}
</script>

<template>
  <div class="avatar-wrap" :style="{ width: `${size}px`, height: `${size}px` }">
    <img v-if="src" class="avatar-img" :src="src" :alt="name || '头像'" />
    <div v-else class="avatar-fallback" :style="{ fontSize: `${Math.round(size * 0.4)}px` }">
      {{ initial }}
    </div>

    <template v-if="editable">
      <button class="avatar-edit" type="button" :disabled="busy" title="更换头像" @click="pick">
        {{ busy ? '…' : '改' }}
      </button>
      <button
        v-if="src"
        class="avatar-clear"
        type="button"
        :disabled="busy"
        title="清除头像"
        @click="clear"
      >×</button>
      <input ref="input" class="avatar-input" type="file" accept="image/*" @change="onChange" />
    </template>
  </div>
</template>

<style scoped>
.avatar-wrap { position: relative; flex: 0 0 auto; }
.avatar-img,
.avatar-fallback {
  width: 100%;
  height: 100%;
  border-radius: 50%;
  object-fit: cover;
  display: flex;
  align-items: center;
  justify-content: center;
}
.avatar-fallback {
  background: linear-gradient(135deg, #0071e3, #4aa8ff);
  color: #fff;
  font-weight: 500;
  user-select: none;
}
.avatar-edit,
.avatar-clear {
  position: absolute;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 0;
  border-radius: 50%;
  background: rgba(0, 0, 0, .62);
  color: #fff;
  cursor: pointer;
  opacity: 0;
  transition: opacity .16s;
}
.avatar-edit {
  inset: 0;
  font-size: 12px;
}
.avatar-clear {
  top: -3px;
  right: -3px;
  width: 18px;
  height: 18px;
  font-size: 13px;
  line-height: 1;
}
.avatar-wrap:hover .avatar-edit,
.avatar-wrap:hover .avatar-clear { opacity: 1; }
.avatar-edit:disabled { cursor: progress; }
.avatar-input { display: none; }
</style>
