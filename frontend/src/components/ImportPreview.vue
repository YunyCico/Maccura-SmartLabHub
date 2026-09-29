<script setup lang="ts">
import { computed, ref } from 'vue'
import { confirmImport, previewImport, type ImportBatch, type ImportPreviewResult } from '../api/imports'

const fileInput = ref<HTMLInputElement | null>(null)
const file = ref<File | null>(null)
const result = ref<ImportPreviewResult | null>(null)
const selectedSheet = ref(0)
const loading = ref(false)
const error = ref('')
const importedBatch = ref<ImportBatch | null>(null)

const sheet = computed(() => result.value?.sheets[selectedSheet.value] ?? null)

function openPicker() {
  fileInput.value?.click()
}

function selectFile(event: Event) {
  const input = event.target as HTMLInputElement
  file.value = input.files?.[0] ?? null
  result.value = null
  importedBatch.value = null
  error.value = ''
}

async function runPreview() {
  if (!file.value) {
    error.value = '请先选择一个 Excel 或 CSV 文件。'
    return
  }
  loading.value = true
  error.value = ''
  try {
    result.value = await previewImport(file.value)
    selectedSheet.value = 0
  } catch (previewError) {
    error.value = previewError instanceof Error ? previewError.message : '文件预览失败，请检查后端服务。'
  } finally {
    loading.value = false
  }
}

async function confirmSelectedImport() {
  if (!file.value || !result.value) return
  loading.value = true
  error.value = ''
  try {
    importedBatch.value = await confirmImport(file.value)
  } catch (importError) {
    error.value = importError instanceof Error ? importError.message : '确认导入失败，请检查后端服务。'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <section class="import-view">
    <div class="import-hero">
      <div>
        <p class="section-kicker">SOURCE INTAKE / PREVIEW ONLY</p>
        <h2>先看清数据，再决定是否进入正式流程。</h2>
        <p class="intro-copy">支持多工作表、多层表头和信息表识别。当前操作只读取文件内容，不修改原始文件和数据库。</p>
      </div>
      <div class="import-rule"><span>STEP 01</span><strong>预览</strong><small>尚未写入</small></div>
    </div>

    <div class="upload-panel panel">
      <input ref="fileInput" class="visually-hidden" type="file" accept=".xlsx,.xlsm,.xltx,.xltm,.csv,.txt" @change="selectFile" />
      <button class="drop-zone" type="button" @click="openPicker">
        <span class="drop-icon">↑</span>
        <strong>{{ file ? file.name : '选择一份数据文件' }}</strong>
        <span>{{ file ? '已选择，点击可更换' : '支持 .xlsx / .xlsm / .csv / .txt' }}</span>
      </button>
      <div class="upload-actions">
        <span class="upload-note">原始文件保持不动 · 只生成内存预览</span>
        <div class="upload-button-group">
          <button class="secondary-button" type="button" :disabled="loading" @click="runPreview">{{ loading ? '处理中…' : '重新预览' }}</button>
          <button class="primary-button" type="button" :disabled="loading || !result || !!importedBatch" @click="confirmSelectedImport">{{ importedBatch ? '已确认导入' : '确认导入' }} <span>↗</span></button>
        </div>
      </div>
      <p v-if="error" class="form-error">{{ error }}</p>
    </div>

    <div v-if="result" class="preview-result">
      <div v-if="importedBatch" class="imported-banner"><span class="status-dot"></span><strong>已完成确认导入</strong><span>{{ importedBatch.original_filename }} · {{ importedBatch.total_rows }} 行 · 批次 #{{ importedBatch.id }}</span></div>
      <div class="preview-summary">
        <div><span>文件</span><strong>{{ result.filename }}</strong></div>
        <div><span>工作表</span><strong>{{ result.sheet_count }}</strong></div>
        <div><span>有效行</span><strong>{{ result.total_rows }}</strong></div>
        <div><span>最大字段数</span><strong>{{ result.max_columns }}</strong></div>
      </div>

      <div class="sheet-tabs" role="tablist" aria-label="工作表">
        <button v-for="(item, index) in result.sheets" :key="item.name" class="sheet-tab" :class="{ active: index === selectedSheet }" type="button" @click="selectedSheet = index">
          {{ item.name }} <small>{{ item.rows }}</small>
        </button>
      </div>

      <section v-if="sheet" class="panel sheet-panel">
        <div class="panel-heading">
          <div>
            <p class="section-kicker">SHEET INSPECTION / {{ sheet.kind === 'info' ? 'INFO TABLE' : 'TABULAR DATA' }}</p>
            <h3>{{ sheet.name }}</h3>
          </div>
          <span class="header-badge">表头行 {{ sheet.header_rows }}</span>
        </div>
        <div class="detected-row">
          <span class="detected-title">字段识别</span>
          <span v-for="(column, role) in sheet.detected_fields" :key="role" class="detected-tag">{{ role }} · {{ column }}</span>
          <span v-if="Object.keys(sheet.detected_fields).length === 0" class="muted-tag">暂未识别关键字段</span>
        </div>
        <div v-if="sheet.warnings.length" class="warning-list">
          <span class="notice-mark">!</span>
          <span>{{ sheet.warnings.join('；') }}。这是预览提示，不会阻止后续查看。</span>
        </div>
        <div class="table-scroll">
          <table class="data-table">
            <thead><tr><th v-for="column in sheet.columns" :key="column">{{ column }}</th></tr></thead>
            <tbody>
              <tr v-for="(row, rowIndex) in sheet.preview" :key="rowIndex"><td v-for="column in sheet.columns" :key="column">{{ row[column] }}</td></tr>
            </tbody>
          </table>
          <div v-if="sheet.preview.length === 0" class="empty-table">没有可展示的数据行</div>
        </div>
      </section>
    </div>
  </section>
</template>
