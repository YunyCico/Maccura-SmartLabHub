<script setup lang="ts">
import { computed, ref } from 'vue'
import { api } from '../api/http'

type Field = { label: string; key: string; group: string; required: boolean; type: string }
type Mapping = { source: string; target_key: string; target_label: string; rule: string }
type PreviewResponse = { session_id: string; filename: string; sheet: string; header_row: number; headers: string[]; row_count: number; mapping: Mapping[]; fields: Field[]; preview: unknown[][]; format_note?: string }

const fileInput = ref<HTMLInputElement | null>(null)
const file = ref<File | null>(null)
const busy = ref(false)
const error = ref('')
const notice = ref('')
const result = ref<Record<string, any> | null>(null)
const data = ref<PreviewResponse | null>(null)
const headerRow = ref(1)
const lab = ref('')
const mapping = ref<Mapping[]>([])
const mappingChanged = ref(false)
const standardPreview = ref<Record<string, unknown>[]>([])
const fields = computed(() => data.value?.fields ?? [])
const headers = computed(() => data.value?.headers ?? [])
const mappedRequired = computed(() => {
  const set = new Set(mapping.value.map(item => item.target_key).filter(Boolean))
  return fields.value.filter(item => item.required && item.key !== 'record_id' && !set.has(item.key)).map(item => item.label)
})

function selectFile() { fileInput.value?.click() }
async function onFileChange(event: Event) {
  const target = event.target as HTMLInputElement
  const chosen = target.files?.[0]
  if (!chosen) return
  file.value = chosen
  await previewFile()
}
async function previewFile() {
  if (!file.value) return
  busy.value = true; error.value = ''; notice.value = ''; result.value = null
  try {
    const form = new FormData()
    form.append('file', file.value)
    const response = await api.post('/data-analysis/lis/preview', form, { timeout: 120000 })
    data.value = response.data
    mapping.value = response.data.mapping
    headerRow.value = response.data.header_row
    mappingChanged.value = false
    await buildPreview()
  } catch (e: any) {
    error.value = e?.response?.data?.detail || '文件识别失败，请检查格式后重试。'
  } finally { busy.value = false }
}
function updateMap(index: number, key: string) {
  const item = mapping.value[index]
  item.target_key = key
  item.target_label = fields.value.find(field => field.key === key)?.label ?? ''
  item.rule = key ? '人工确认' : '不导入'
  mappingChanged.value = true
  buildPreview()
}
async function buildPreview() {
  if (!data.value) return
  const sourceIndex = new Map(headers.value.map((header, index) => [header, index]))
  const fieldsByKey = new Map(fields.value.map(field => [field.key, field]))
  standardPreview.value = (data.value.preview || []).slice(0, 10).map((row, rowIndex) => {
    const record: Record<string, unknown> = {}
    for (const field of fields.value) record[field.label] = null
    const recordIdField = fieldsByKey.get('record_id')
    if (recordIdField) record[recordIdField.label] = `预览-${rowIndex + 1}`
    for (const item of mapping.value) {
      if (!item.target_key) continue
      const index = sourceIndex.get(item.source)
      if (index !== undefined) record[fieldsByKey.get(item.target_key)?.label ?? item.target_key] = (row as unknown[])[index] ?? null
    }
    record['来源文件'] = data.value?.filename
    return record
  })
}
async function setHeaderRow() {
  if (!file.value || !headerRow.value) return
  busy.value = true; error.value = ''
  try {
    const form = new FormData(); form.append('file', file.value)
    const response = await api.post(`/data-analysis/lis/preview?header_row=${encodeURIComponent(headerRow.value)}`, form, { timeout: 120000 })
    data.value = response.data; mapping.value = response.data.mapping; mappingChanged.value = false
    await buildPreview()
  } catch (e: any) { error.value = e?.response?.data?.detail || '重新识别表头失败。' }
  finally { busy.value = false }
}
async function confirmSave() {
  if (!data.value || mappedRequired.value.length) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const response = await api.post('/data-analysis/lis/confirm', {
      session_id: data.value.session_id, header_row: headerRow.value, lab: lab.value,
      mapping: mapping.value,
    }, { timeout: 120000 })
    result.value = response.data
    notice.value = '标准化副本已保存。原始 LIS 文件未修改。'
  } catch (e: any) { error.value = e?.response?.data?.detail || '保存失败，请检查必填字段映射。' }
  finally { busy.value = false }
}
function reset() {
  file.value = null; data.value = null; mapping.value = []; result.value = null; notice.value = ''; error.value = ''; lab.value = ''
  if (fileInput.value) fileInput.value.value = ''
}
</script>

<template>
  <section class="lis-organizer">
    <header class="lis-heading">
      <div><p class="section-kicker">标准模板 v1.1 · 44 个中文字段</p><h2>LIS 数据整理</h2><p>上传 LIS 导出表，审核字段对照与标准化预览后，再确认保存。</p></div>
      <div class="lis-template-pill">源文件只读 · 标准化结果单独保存</div>
    </header>

    <div class="lis-card lis-upload-card">
      <input ref="fileInput" class="lis-file-input" type="file" accept=".xlsx,.xlsm,.csv,.xls" @change="onFileChange" />
      <div class="lis-upload-copy"><strong>{{ file?.name || '选择 LIS 导出文件' }}</strong><span>支持 XLS、XLSX、XLSM、CSV（最大 100 MB）；按文件内容自动识别真实格式，标准化结果另存为 XLSX</span></div>
      <button class="primary-button" type="button" :disabled="busy" @click="selectFile">{{ busy ? '处理中…' : '选择文件' }}</button>
      <button v-if="data" class="ghost-button" type="button" @click="reset">重新开始</button>
    </div>
    <p v-if="error" class="form-error">{{ error }}</p>
    <p v-if="notice" class="lis-success">{{ notice }}　批次：{{ result?.batch_id }}；行数：{{ result?.rows }}；异常行：{{ result?.quality_issue_count }}。<a class="lis-download" :href="`/api/data-analysis/lis/download/${result?.batch_id}`">下载标准化 Excel</a></p>

    <template v-if="data">
      <p v-if="data.format_note" class="lis-format-note">已自动识别真实格式：{{ data.format_note }}</p>
      <div class="lis-card lis-file-summary">
        <div><span>文件</span><strong>{{ data.filename }}</strong></div><div><span>工作表</span><strong>{{ data.sheet }}</strong></div><div><span>识别数据行</span><strong>{{ data.row_count.toLocaleString() }}</strong></div>
        <label>表头行号<input v-model.number="headerRow" type="number" min="1" max="100" @change="setHeaderRow" /></label>
        <label>实验室（可选）<input v-model="lab" type="text" placeholder="如：佳佳" /></label>
      </div>
      <div class="lis-card">
        <div class="lis-section-title"><div><h3>字段对照审核</h3><p>可修改每个源字段对应的标准字段；未导入的列请选择“不导入”。</p></div><span>{{ mappingChanged ? '映射已修改' : '系统建议，可编辑' }}</span></div>
        <div class="lis-table-wrap"><table class="lis-map-table"><thead><tr><th>源字段</th><th>标准字段（中文）</th><th>处理规则</th><th>必填状态</th></tr></thead><tbody>
          <tr v-for="(item, index) in mapping" :key="`${item.source}-${index}`"><td><strong>{{ item.source }}</strong></td><td><select :value="item.target_key" @change="updateMap(index, ($event.target as HTMLSelectElement).value)"><option value="">不导入</option><option v-for="field in fields" :key="field.key" :value="field.key">{{ field.label }}{{ field.required ? '（必填）' : '' }}</option></select></td><td>{{ item.rule || '原值保留' }}</td><td>{{ fields.find(field => field.key === item.target_key)?.required ? '必填' : '选填' }}</td></tr>
        </tbody></table></div>
        <p v-if="mappedRequired.length" class="lis-warning">还需映射必填字段：{{ mappedRequired.join('、') }}</p>
      </div>
      <div class="lis-card">
        <div class="lis-section-title"><div><h3>标准化结果预览</h3><p>展示前 10 行；日期按标准格式转换，负数数值留空，原始文件和数据不被覆盖。</p></div><span>{{ standardPreview.length }} 行预览</span></div>
        <div class="lis-table-wrap"><table class="lis-preview-table"><thead><tr><th v-for="field in fields" :key="field.key">{{ field.label }}</th></tr></thead><tbody><tr v-for="(row, i) in standardPreview" :key="i"><td v-for="field in fields" :key="field.key">{{ row[field.label] ?? '—' }}</td></tr></tbody></table></div>
      </div>
      <div class="lis-confirm-row"><span>确认后会生成标准化 Excel 副本，并保存映射方案和源文件摘要用于追溯。</span><button class="primary-button" type="button" :disabled="busy || mappedRequired.length > 0" @click="confirmSave">{{ busy ? '保存中…' : '确认并保存标准化结果' }}</button></div>
      <div v-if="result?.quality_issues?.length" class="lis-card lis-quality"><h3>质量校验提示</h3><p>部分数据行缺少必填值或存在待核对内容，共 {{ result.quality_issue_count }} 行；已保存的异常清单展示前 100 行。</p><ul><li v-for="issue in result.quality_issues" :key="issue.row">源数据第 {{ issue.row }} 行：缺少 {{ issue.missing.join('、') }}</li></ul></div>
    </template>
  </section>
</template>
