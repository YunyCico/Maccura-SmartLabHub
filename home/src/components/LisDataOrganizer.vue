<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '../api/http'

type Field = { label: string; key: string; group: string; required: boolean; type: string }
type FieldDef = Field & { desc: string; source: string; isNew?: boolean; marked?: boolean }
type FieldStatus = { fields: FieldDef[]; customized: boolean; types: string[]; groups: string[] }
type Mapping = { source: string; target_key: string; target_label: string; rule: string }
type PreviewResponse = { session_id: string; filename: string; sheet: string; header_row: number; headers: string[]; row_count: number; mapping: Mapping[]; fields: Field[]; preview: unknown[][]; format_note?: string }
type ImportedSheet = { name: string; rows: number; columns: string[] }
type ImportedSource = { id: string; name: string; size: number; sheet_count: number; total_rows: number; sheets: ImportedSheet[] }
type StandardPreview = {
  batch_id: string
  columns: string[]
  column_keys: string[]
  rows: unknown[][]
  total_rows: number
  page: number
  page_size: number
  total_pages: number
  hidden_empty_columns: number
  quality_issue_count: number
  quality_issues: { row: number; missing: string[] }[]
  missing_required: string[]
}
type SavedBatch = {
  batch_id: string
  source_filename: string
  sheet: string
  lab: string
  row_count: number
  column_count: number
  created_at: string
  output_file: string
  available: boolean
}

const fileInput = ref<HTMLInputElement | null>(null)
const file = ref<File | null>(null)
const importedSources = ref<ImportedSource[]>([])
const selectedSourceId = ref('')
const selectedSheet = ref('')
const busy = ref(false)
const error = ref('')
const notice = ref('')
const result = ref<Record<string, any> | null>(null)
const data = ref<PreviewResponse | null>(null)
const headerRow = ref(1)
const lab = ref('')
const mapping = ref<Mapping[]>([])
const mappingChanged = ref(false)
const savedBatches = ref<SavedBatch[]>([])

/** 字段对应是否已确认。确认后才生成预览（预览由后端按当前映射构建，与保存结果一致）。 */
const mappingConfirmed = ref(false)
const standard = ref<StandardPreview | null>(null)
const page = ref(1)
const pageSize = ref(50)

const selectedSource = computed(() => importedSources.value.find(source => source.id === selectedSourceId.value) ?? null)
const importedSheets = computed(() => selectedSource.value?.sheets ?? [])
// 字段对照审核的下拉：优先用「标准字段设置」里的**生效定义**（保存后立刻生效，不用重新预览）
const fields = computed(() => (fieldStatus.value?.fields?.length ? fieldStatus.value.fields : (data.value?.fields ?? [])))
const fieldCount = computed(() => fields.value.length)
const requiredCount = computed(() => fields.value.filter(f => f.required).length)

// ---------------------------------------------------------------- 标准字段设置
const fieldStatus = ref<FieldStatus | null>(null)
const editingFields = ref<FieldDef[]>([])
const showFieldSettings = ref(false)
const fieldBusy = ref(false)
const fieldError = ref('')
const fieldNotice = ref('')
/** 系统自动填的字段，不允许删（专业/组套由试剂分类表回填） */
const PROTECTED_KEYS = ['test_specialty', 'test_suite']

async function loadFieldStatus() {
  try {
    const r = await api.get('/data-analysis/lis/fields')
    fieldStatus.value = r.data
  } catch (e) {
    // 读不到就用预览返回的字段，不打扰用户
  }
}
async function openFieldSettings() {
  fieldError.value = ''
  fieldNotice.value = ''
  fieldBusy.value = true
  try {
    await loadFieldStatus()
    editingFields.value = (fieldStatus.value?.fields ?? []).map(f => ({ ...f, isNew: false }))
    showFieldSettings.value = true
  } catch (e: any) {
    fieldError.value = e?.response?.data?.detail || '读取标准字段失败'
  } finally {
    fieldBusy.value = false
  }
}
function addFieldRow() {
  editingFields.value.push({
    label: '', key: '', group: '自定义', required: false,
    type: '文本', desc: '', source: '', isNew: true, marked: false,
  })
}
/** 删除改为「先标记、保存后生效」：点一次画横线，再点一次撤销。 */
function toggleFieldMark(index: number) {
  const row = editingFields.value[index]
  if (!row) return
  row.marked = !row.marked
}
const markedCount = computed(() => editingFields.value.filter(f => f.marked).length)
async function saveFieldSettings() {
  const kept = editingFields.value.filter(f => !f.marked)
  // 本次真正删掉的（已存在的字段被标记）—— 用本地标记数，不看后端 removed
  const removing = editingFields.value.filter(f => f.marked && !f.isNew).length
  if (!kept.length) {
    fieldError.value = '至少要保留一个字段。'
    return
  }
  // 新增行必须把中文名和英文字段名都填上，否则后端会静默跳过
  const incomplete = kept.find(f => f.isNew && (!f.label.trim() || !f.key.trim()))
  if (incomplete) {
    fieldError.value = `新增的字段「${incomplete.label || '未命名'}」还没填${!incomplete.label.trim() ? '列名(中文)' : '英文字段名'}，填好或标记删除后再保存。`
    return
  }
  fieldBusy.value = true
  fieldError.value = ''
  fieldNotice.value = ''
  try {
    const payload = kept.map(f => ({
      label: f.label, key: f.key, group: f.group,
      required: !!f.required, type: f.type, desc: f.desc, source: f.source,
    }))
    const r = await api.post('/data-analysis/lis/fields', { fields: payload })
    fieldStatus.value = r.data.status
    editingFields.value = (r.data.status?.fields ?? []).map((f: FieldDef) => ({ ...f, isNew: false, marked: false }))
    const count = r.data.status?.fields?.length ?? 0
    fieldNotice.value = removing
      ? `已保存，删除 ${removing} 个字段，当前 ${count} 个字段`
      : `已保存，当前 ${count} 个字段`
    notice.value = removing
      ? `标准字段已保存 —— 删掉的 ${removing} 个字段，已保存记录里指向它的映射自动改成「不导入」。`
      : '标准字段已保存 —— 字段对照审核里的可选项、标准化结果的列都会跟着变。'
  } catch (e: any) {
    fieldError.value = e?.response?.data?.detail || '保存失败'
  } finally {
    fieldBusy.value = false
  }
}
async function resetFieldSettings() {
  fieldBusy.value = true
  fieldError.value = ''
  fieldNotice.value = ''
  try {
    const r = await api.post('/data-analysis/lis/fields/reset')
    fieldStatus.value = r.data.status
    editingFields.value = (r.data.status?.fields ?? []).map((f: FieldDef) => ({ ...f, isNew: false }))
    fieldNotice.value = '已恢复出厂定义'
  } catch (e: any) {
    fieldError.value = e?.response?.data?.detail || '恢复失败'
  } finally {
    fieldBusy.value = false
  }
}
const previewColumns = computed(() => standard.value?.columns ?? [])
const pageStart = computed(() => {
  const total = standard.value?.total_rows ?? 0
  if (!total) return 0
  return (standard.value!.page - 1) * standard.value!.page_size + 1
})
const pageEnd = computed(() => {
  const standardValue = standard.value
  if (!standardValue) return 0
  return Math.min(standardValue.page * standardValue.page_size, standardValue.total_rows)
})

function selectFile() { fileInput.value?.click() }

async function loadImportedSources() {
  try {
    const response = await api.get('/data-analysis/lis/imported-sources')
    importedSources.value = response.data.sources ?? []
  } catch (e: any) {
    importedSources.value = []
    error.value = e?.response?.data?.detail || '已导入数据加载失败。'
  }
}
async function loadSavedBatches() {
  try {
    const response = await api.get('/data-analysis/lis/saved-batches')
    savedBatches.value = response.data.batches ?? []
  } catch {
    savedBatches.value = []
  }
}

/** 删除一条已整理记录：先二次确认，删完刷新列表。 */
async function deleteSavedBatch(batch: SavedBatch) {
  const label = batch.source_filename || batch.batch_id
  if (!window.confirm(`确定删除这条已整理记录吗？\n\n源文件：${label}\n保存时间：${batch.created_at || '—'}\n行数：${batch.row_count.toLocaleString()}\n\n删除后该记录将从列表移除，对应的标准化 Excel 文件也会一并删除，且不可恢复。`)) return
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    await api.delete(`/data-analysis/lis/saved-batches/${batch.batch_id}`)
    notice.value = `已删除「${label}」。`
    await loadSavedBatches()
  } catch (e: any) {
    error.value = e?.response?.data?.detail || '删除失败，请重试。'
  } finally {
    busy.value = false
  }
}
async function onFileChange(event: Event) {
  const target = event.target as HTMLInputElement
  const chosen = target.files?.[0]
  if (!chosen) return
  file.value = chosen
  selectedSourceId.value = ''; selectedSheet.value = ''
  await previewFile()
}
async function previewFile() {
  if (!file.value) return
  busy.value = true; error.value = ''; notice.value = ''; result.value = null
  standard.value = null; mappingConfirmed.value = false
  try {
    const form = new FormData()
    form.append('file', file.value)
    const response = await api.post('/data-analysis/lis/preview', form, { timeout: 120000 })
    applyPreview(response.data)
  } catch (e: any) {
    error.value = e?.response?.data?.detail || '文件识别失败，请检查格式后重试。'
  } finally { busy.value = false }
}
function applyPreview(response: PreviewResponse) {
  data.value = response
  mapping.value = response.mapping
  headerRow.value = response.header_row
  mappingChanged.value = false
  mappingConfirmed.value = false
  standard.value = null
  page.value = 1
}
async function previewImported() {
  if (!selectedSourceId.value) return
  busy.value = true; error.value = ''; notice.value = ''; result.value = null
  standard.value = null; mappingConfirmed.value = false
  try {
    const params = new URLSearchParams({ dataset_id: selectedSourceId.value })
    if (selectedSheet.value) params.set('sheet', selectedSheet.value)
    const response = await api.post(`/data-analysis/lis/preview-imported?${params.toString()}`, undefined, { timeout: 120000 })
    file.value = null
    applyPreview(response.data)
  } catch (e: any) {
    error.value = e?.response?.data?.detail || '已导入数据识别失败，请检查源文件。'
  } finally { busy.value = false }
}
function onSourceChange() {
  selectedSheet.value = importedSheets.value[0]?.name ?? ''
}
function updateMap(index: number, key: string) {
  const item = mapping.value[index]
  item.target_key = key
  item.target_label = fields.value.find(field => field.key === key)?.label ?? ''
  item.rule = key ? '人工确认' : '不导入'
  mappingChanged.value = true
  // 映射一改，之前确认过的预览就作废了，必须重新确认
  mappingConfirmed.value = false
  standard.value = null
}

/** 重新识别表头行：换了表头，前面确认过的预览作废。 */
async function setHeaderRow() {
  if (!data.value || !headerRow.value) return
  busy.value = true; error.value = ''
  standard.value = null; mappingConfirmed.value = false
  try {
    if (selectedSourceId.value) {
      const params = new URLSearchParams({ dataset_id: selectedSourceId.value, header_row: String(headerRow.value) })
      if (selectedSheet.value) params.set('sheet', selectedSheet.value)
      const response = await api.post(`/data-analysis/lis/preview-imported?${params.toString()}`, undefined, { timeout: 120000 })
      applyPreview(response.data)
    } else if (file.value) {
      const form = new FormData(); form.append('file', file.value)
      const response = await api.post(`/data-analysis/lis/preview?header_row=${encodeURIComponent(headerRow.value)}`, form, { timeout: 120000 })
      applyPreview(response.data)
    }
  } catch (e: any) { error.value = e?.response?.data?.detail || '重新识别表头失败。' }
  finally { busy.value = false }
}

/** 确认字段对应 → 调后端生成标准化预览（可翻页看全部数据）。 */
async function confirmMapping() {
  if (!data.value) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const response = await api.post('/data-analysis/lis/preview-standard', {
      session_id: data.value.session_id,
      header_row: headerRow.value,
      lab: lab.value,
      mapping: mapping.value,
      page: page.value,
      page_size: pageSize.value,
    }, { timeout: 300000 })
    standard.value = response.data
    mappingConfirmed.value = true
    page.value = response.data.page ?? 1
  } catch (e: any) {
    error.value = e?.response?.data?.detail || '生成预览失败，请检查字段对应后重试。'
  } finally { busy.value = false }
}

async function gotoPage(next: number) {
  if (!data.value) return
  const total = standard.value?.total_pages ?? 1
  if (next < 1 || next > total) return
  page.value = next
  await confirmMapping()
}

async function changePageSize() {
  page.value = 1
  await confirmMapping()
}

async function confirmSave() {
  if (!data.value) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const response = await api.post('/data-analysis/lis/confirm', {
      session_id: data.value.session_id, header_row: headerRow.value, lab: lab.value,
      mapping: mapping.value,
    }, { timeout: 300000 })
    result.value = response.data
    notice.value = '标准化副本已保存。原始 LIS 文件未修改。'
    await loadSavedBatches()
  } catch (e: any) { error.value = e?.response?.data?.detail || '保存失败，请重试。' }
  finally { busy.value = false }
}

/** 直接导出当前映射的标准化 Excel（不写入"已整理"记录）。 */
async function exportExcel() {
  if (!data.value) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const response = await api.post('/data-analysis/lis/export', {
      session_id: data.value.session_id, header_row: headerRow.value, lab: lab.value,
      mapping: mapping.value,
    }, { timeout: 300000, responseType: 'blob' })
    const blob = new Blob([response.data], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `LIS标准化导出_${data.value.filename.replace(/\.[^.]+$/, '')}.xlsx`
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(url)
    notice.value = '标准化 Excel 已导出。'
  } catch (e: any) {
    error.value = e?.response?.data?.detail || '导出失败，请重试。'
  } finally { busy.value = false }
}

function reset() {
  file.value = null; selectedSourceId.value = ''; selectedSheet.value = ''
  data.value = null; mapping.value = []; result.value = null; notice.value = ''; error.value = ''; lab.value = ''
  standard.value = null; mappingConfirmed.value = false
  if (fileInput.value) fileInput.value.value = ''
}
onMounted(async () => {
  await Promise.all([loadImportedSources(), loadSavedBatches()])
})
</script>

<template>
  <section class="lis-organizer">
    <header class="lis-heading">
      <div><p class="section-kicker">标准模板 v1.2 · {{ fieldCount }} 个中文字段 · {{ requiredCount ? requiredCount + ' 个必填' : '不设必填' }}</p><h2>LIS 数据整理</h2><p>上传 LIS 导出表，审核字段对应后确认，再预览全部数据并导出或保存。</p></div>
      <div class="lis-heading-right">
        <button class="secondary-button" type="button" :disabled="fieldBusy" @click="openFieldSettings">标准字段设置</button>
        <div class="lis-template-pill">源文件只读 · 标准化结果单独保存</div>
      </div>
    </header>

    <div class="lis-card lis-upload-card">
      <input ref="fileInput" class="lis-file-input" type="file" accept=".xlsx,.xlsm,.csv,.xls" @change="onFileChange" />
      <div class="lis-upload-copy"><strong>{{ file?.name || selectedSource?.name || '选择 LIS 导出文件' }}</strong><span>可以选择本地文件，也可以直接复用“数据源管理”里已导入的源文件；标准化结果另存为 XLSX</span></div>
      <button class="secondary-button" type="button" :disabled="busy" @click="selectFile">{{ busy ? '处理中…' : '选择本地文件' }}</button>
      <button v-if="data" class="ghost-button" type="button" @click="reset">重新开始</button>
    </div>
    <div class="lis-card lis-imported-card">
      <div class="lis-section-title"><div><h3>选择已导入的数据表</h3><p>不需要再次从电脑选择文件，直接读取数据源管理中的原始副本。</p></div><span>{{ importedSources.length }} 份可用</span></div>
      <div class="lis-source-picker">
        <select v-model="selectedSourceId" :disabled="busy || !importedSources.length" @change="onSourceChange"><option value="">请选择已导入数据表</option><option v-for="source in importedSources" :key="source.id" :value="source.id">{{ source.name }}（{{ source.total_rows.toLocaleString() }} 行）</option></select>
        <select v-if="selectedSourceId && importedSheets.length > 1" v-model="selectedSheet" :disabled="busy" @change="previewImported"><option v-for="sheet in importedSheets" :key="sheet.name" :value="sheet.name">工作表：{{ sheet.name }}（{{ sheet.rows.toLocaleString() }} 行）</option></select>
        <button class="primary-button" type="button" :disabled="busy || !selectedSourceId" @click="previewImported">读取已导入表</button>
      </div>
      <p v-if="!importedSources.length" class="lis-empty-source">当前没有可复用的数据源，请先到“数据源管理”导入。</p>
    </div>
    <p v-if="error" class="form-error">{{ error }}</p>
    <p v-if="notice" class="lis-success">{{ notice }}<template v-if="result?.batch_id">　批次：{{ result?.batch_id }}；行数：{{ result?.rows }}；有效列：{{ result?.column_count }}；隐藏空列：{{ result?.hidden_empty_columns }}。<a class="lis-download" :href="`/api/data-analysis/lis/download/${result?.batch_id}`">下载标准化 Excel</a></template></p>

    <template v-if="data">
      <p v-if="data.format_note" class="lis-format-note">已自动识别真实格式：{{ data.format_note }}</p>
      <div class="lis-card lis-file-summary">
        <div><span>文件</span><strong>{{ data.filename }}</strong></div><div><span>工作表</span><strong>{{ data.sheet }}</strong></div><div><span>识别数据行</span><strong>{{ data.row_count.toLocaleString() }}</strong></div>
        <label>表头行号<input v-model.number="headerRow" type="number" min="1" max="100" @change="setHeaderRow" /></label>
        <label>实验室（可选）<input v-model="lab" type="text" placeholder="如：佳佳" /></label>
      </div>
      <div class="lis-card">
        <div class="lis-section-title"><div><h3>字段对照审核</h3><p>可修改每个源字段对应的标准字段；未导入的列请选择“不导入”。源表没有的字段留空即可，不影响保存。</p></div><span>{{ mappingChanged ? '映射已修改' : '系统建议，可编辑' }}</span></div>
        <div class="lis-table-wrap"><table class="lis-map-table"><thead><tr><th>源字段</th><th>标准字段（中文）</th><th>处理规则</th><th>是否已映射</th></tr></thead><tbody>
          <tr v-for="(item, index) in mapping" :key="`${item.source}-${index}`"><td><strong>{{ item.source }}</strong></td><td><select :value="item.target_key" @change="updateMap(index, ($event.target as HTMLSelectElement).value)"><option value="">不导入</option><option v-for="field in fields" :key="field.key" :value="field.key">{{ field.label }}</option></select></td><td>{{ item.rule || '原值保留' }}</td><td>{{ item.target_key ? '已映射' : '未映射' }}</td></tr>
        </tbody></table></div>
        <p class="lis-tip">不设必填：源表有的就填，没有的留空；专业 / 组套由检测项目名称与项目英文名称自动对照试剂分类表填充。</p>
        <div class="lis-confirm-row lis-confirm-mapping">
          <span>{{ mappingConfirmed ? '字段对应已确认，下方是全量数据的标准化预览。改动任何对应关系都需要重新确认。' : '确认字段对应后，下方才会出现标准化预览（可翻页浏览全部数据行）。' }}</span>
          <button class="primary-button" type="button" :disabled="busy" @click="confirmMapping">
            {{ busy ? '处理中…' : (mappingConfirmed ? '重新确认并刷新预览' : '确认字段对应并预览') }}
          </button>
        </div>
      </div>

      <div v-if="standard" class="lis-card">
        <div class="lis-section-title"><div><h3>标准化结果预览</h3><p>可翻页浏览全部 {{ standard.total_rows.toLocaleString() }} 行；全空列不显示；日期按标准格式转换，负数数值留空，原始文件和数据不被覆盖。</p></div><span>{{ standard.total_rows.toLocaleString() }} 行 · {{ standard.columns.length }} 列 · 隐藏空列 {{ standard.hidden_empty_columns }}</span></div>
        <div class="lis-preview-toolbar">
          <div class="lis-pager">
            <button class="secondary-button" type="button" :disabled="busy || standard.page <= 1" @click="gotoPage(standard.page - 1)">上一页</button>
            <span>第 {{ standard.page }} / {{ standard.total_pages }} 页 · 显示第 {{ pageStart.toLocaleString() }}–{{ pageEnd.toLocaleString() }} 行</span>
            <button class="secondary-button" type="button" :disabled="busy || standard.page >= standard.total_pages" @click="gotoPage(standard.page + 1)">下一页</button>
            <label class="lis-page-size">每页
              <select :value="pageSize" @change="changePageSize">
                <option :value="50">50 行</option>
                <option :value="100">100 行</option>
                <option :value="200">200 行</option>
                <option :value="500">500 行</option>
              </select>
            </label>
          </div>
          <div class="lis-preview-actions">
            <button class="secondary-button" type="button" :disabled="busy" @click="exportExcel">导出 Excel</button>
            <button class="primary-button" type="button" :disabled="busy" @click="confirmSave">{{ busy ? '保存中…' : '保存整理结果' }}</button>
          </div>
        </div>
        <div class="lis-table-wrap"><table class="lis-preview-table"><thead><tr><th v-for="label in previewColumns" :key="label">{{ label }}</th></tr></thead><tbody><tr v-for="(row, i) in standard.rows" :key="i"><td v-for="(cell, j) in row" :key="j">{{ cell ?? '—' }}</td></tr></tbody></table></div>
        <p v-if="!standard.rows.length" class="lis-empty-source">当前页没有数据。</p>
      </div>
      <div v-if="standard?.quality_issues?.length" class="lis-card lis-quality"><h3>质量校验提示</h3><p>部分数据行存在待核对内容，共 {{ standard.quality_issue_count }} 行；已保存的异常清单展示前 100 行。</p><ul><li v-for="issue in standard.quality_issues" :key="issue.row">源数据第 {{ issue.row }} 行：缺少 {{ issue.missing.join('、') }}</li></ul></div>
    </template>

    <div class="lis-card lis-saved-card">
      <div class="lis-section-title"><div><h3>已整理并保存的数据表格</h3><p>历次确认保存的标准化结果，可直接下载。</p></div><span>{{ savedBatches.length }} 个</span></div>
      <div v-if="savedBatches.length" class="lis-table-wrap"><table class="lis-saved-table"><thead><tr><th>保存时间</th><th>源文件</th><th>工作表</th><th>实验室</th><th>行数</th><th>列数</th><th>操作</th></tr></thead><tbody>
        <tr v-for="batch in savedBatches" :key="batch.batch_id" :data-batch-id="batch.batch_id">
          <td>{{ batch.created_at || '—' }}</td>
          <td><strong>{{ batch.source_filename }}</strong></td>
          <td>{{ batch.sheet || '—' }}</td>
          <td>{{ batch.lab || '—' }}</td>
          <td>{{ batch.row_count.toLocaleString() }}</td>
          <td>{{ batch.column_count }}</td>
          <td class="lis-saved-actions">
            <a v-if="batch.available" class="lis-download" :href="`/api/data-analysis/lis/download/${batch.batch_id}`">下载</a><span v-else class="lis-muted">文件已不在</span>
            <button class="lis-delete-button" type="button" :disabled="busy" @click="deleteSavedBatch(batch)">删除</button>
          </td>
        </tr>
      </tbody></table></div>
      <p v-else class="lis-empty-source">还没有保存过标准化结果。</p>
    </div>

    <!-- ---------------- 标准字段设置 ---------------- -->
    <div v-if="showFieldSettings" class="slh-modal-mask" @click.self="showFieldSettings = false">
      <div class="slh-modal slh-modal-wide">
        <div class="slh-modal-head">
          <div>
            <h3>标准字段设置</h3>
            <p>改的是<b>本机</b>的标准字段定义（出厂模板不动）。「列名(中文)」「说明」「原始数据来源」可以随意改。
              点「删除」先画横线标记、再点一次撤销，<b>保存后</b>才真正删掉。
              新增字段时在中文名下面填英文名（字母开头，字母数字下划线）和分组。</p>
          </div>
          <button class="ghost-button" type="button" @click="showFieldSettings = false">关闭</button>
        </div>
        <div class="slh-modal-body">
          <div class="slh-fs-toolbar">
            <span class="slh-fs-count">{{ editingFields.length }} 个字段
              <span v-if="fieldStatus?.customized" class="slh-fs-badge">已自定义</span>
              <span v-else class="slh-fs-badge slh-fs-badge-mute">出厂定义</span>
            </span>
            <span class="slh-fs-spacer"></span>
            <button class="secondary-button" type="button" :disabled="fieldBusy" @click="addFieldRow">新增字段</button>
            <button class="secondary-button" type="button" :disabled="fieldBusy || !fieldStatus?.customized" @click="resetFieldSettings">恢复出厂定义</button>
            <button class="primary-button" type="button" :disabled="fieldBusy" @click="saveFieldSettings">{{ fieldBusy ? '保存中…' : '保存' }}</button>
          </div>
          <p v-if="fieldError" class="form-error">{{ fieldError }}</p>
          <p v-if="fieldNotice" class="slh-fs-ok">{{ fieldNotice }}</p>
          <p v-if="markedCount" class="slh-fs-marked-tip">已标记 {{ markedCount }} 个字段待删除（画横线），保存后生效；再点一次「撤销」可取消。</p>
          <div class="slh-fs-table-wrap">
            <table class="slh-fs-table">
              <thead><tr>
                <th style="min-width:210px">列名(中文)</th>
                <th style="width:66px">必填</th>
                <th style="width:96px">类型</th>
                <th style="min-width:150px">说明</th>
                <th style="min-width:150px">原始数据来源</th>
                <th style="width:76px"></th>
              </tr></thead>
              <tbody>
                <tr v-for="(f, i) in editingFields" :key="i" :class="{ 'is-marked': f.marked }">
                  <td>
                    <input v-model="f.label" class="slh-fs-in" placeholder="显示用的中文名" />
                    <div v-if="f.isNew" class="slh-fs-newmeta">
                      <input v-model="f.key" class="slh-fs-in slh-fs-key" placeholder="英文字段名（字母开头）" />
                      <input v-model="f.group" class="slh-fs-in" placeholder="分组" />
                    </div>
                  </td>
                  <td class="slh-fs-center"><input v-model="f.required" type="checkbox" /></td>
                  <td>
                    <select v-model="f.type" class="slh-fs-in slh-fs-sel">
                      <option v-for="t in (fieldStatus?.types ?? ['文本', '日期时间', '数值', '枚举'])" :key="t" :value="t">{{ t }}</option>
                    </select>
                  </td>
                  <td><input v-model="f.desc" class="slh-fs-in" placeholder="说明" /></td>
                  <td><input v-model="f.source" class="slh-fs-in" placeholder="源表里的列名" /></td>
                  <td class="slh-fs-center">
                    <span v-if="PROTECTED_KEYS.includes(f.key)" class="slh-fs-lock" title="系统自动填充，不能删除">🔒</span>
                    <button v-else class="lis-delete-button" :class="{ 'is-undo': f.marked }" type="button" :disabled="fieldBusy" @click="toggleFieldMark(i)">{{ f.marked ? '撤销' : '删除' }}</button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <p class="slh-fs-tip">保存后真正删除的字段：如果有已保存的记录正指向它，会自动改成「不导入」，记录不会打不开。</p>
        </div>
      </div>
    </div>
  </section>
</template>
