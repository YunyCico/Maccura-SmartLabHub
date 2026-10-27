<script setup>
import { computed, ref } from 'vue'

const props = defineProps({
  form: Object, busy: Boolean, operators: Array, tableFields: Object
})
const emit = defineEmits(['submit', 'sample'])
const form = computed(() => props.form)

const FIXED_TABLE = 'ADS_SERVICE_DEMAND_FEEDBACK'

/* 兜底字段清单：仅在后端尚未缓存真实表结构时使用（首次拉取后由后端 /api/meta 下发真实字段） */
const DEFAULT_TABLE_FIELDS = [
  { name: 'REQUESTID', comment: '流程ID', values: [] },
  { name: 'CREATEDATE', comment: '创建日期', values: [] },
  { name: 'YJFL', comment: '一级分类', values: [] },
  { name: 'EJFL', comment: '二级分类', values: [] },
  { name: 'ZDKH', comment: '终端客户', values: [] },
  { name: 'DQ', comment: '大区', values: [] },
  { name: 'LASTNAME1', comment: '运营专员', values: [] },
  { name: 'DEPARTMENTNAME1', comment: '所属部门', values: [] },
  { name: 'SELECTNAME5', comment: '问题状态', values: [] },
  { name: 'SELECTNAME7', comment: '客户满意度', values: [] }
]

/* ---------- 筛选条件 ---------- */
const condTables = computed(() => (form.value.tableName || []).length
  ? form.value.tableName
  : [FIXED_TABLE])
const openCond = ref(false)
function rowsOf(table) {
  if (!form.value.condition) form.value.condition = {}
  if (!form.value.condition[table]) form.value.condition[table] = []
  return form.value.condition[table]
}
function addRow(table) { rowsOf(table).push({ field: '', operator: 'eq', value: '' }) }
function delRow(table, i) { rowsOf(table).splice(i, 1) }
function clearCond() { form.value.condition = {} }
function fieldsOf(table) {
  const fields = props.tableFields?.[table]?.length
    ? props.tableFields[table]
    : table === 'ADS_SERVICE_DEMAND_FEEDBACK' ? DEFAULT_TABLE_FIELDS : []
  if (table !== 'ADS_SERVICE_DEMAND_FEEDBACK') return fields
  return fields.map((field) => {
    const fallback = DEFAULT_TABLE_FIELDS.find((item) => item.name === field.name)
    return field.values?.length || !fallback?.values?.length
      ? field
      : { ...field, values: fallback.values }
  })
}
function fieldOf(table, name) {
  return fieldsOf(table).find((field) => field.name === name)
}
function valuesOf(table, name) {
  return fieldOf(table, name)?.values || []
}
function resetValue(table, row) {
  if (!valuesOf(table, row.field).includes(row.value)) row.value = ''
}
const condCount = computed(() =>
  Object.values(form.value.condition || {}).reduce((s, a) => s + a.length, 0)
)
function opNoValue(op) {
  return props.operators?.find((o) => o.value === op)?.noValue ?? false
}
const FIELD_RE = /^[A-Za-z_][A-Za-z0-9_]*$/
function fieldOk(v) { return FIELD_RE.test((v || '').trim()) }
function condInvalid(table) {
  return rowsOf(table).some((r) =>
    !fieldOk(r.field) || (!opNoValue(r.operator) &&
      !(Array.isArray(r.value) ? r.value.length : String(r.value || '').trim()))
  )
}
const condValid = computed(() =>
  condTables.value.length > 0 && condTables.value.every((t) => !condInvalid(t))
)

/* ---------- 分页 ---------- */
const PAGE_MAX = 10000
function clampPage() {
  let no = Number(form.value.pageNo), ps = Number(form.value.pageSize)
  if (!no || !ps) return
  no = Math.max(1, Math.floor(no)); ps = Math.min(PAGE_MAX, Math.max(1, Math.floor(ps)))
  form.value.pageNo = no; form.value.pageSize = ps
}
function clearPage() { form.value.pageNo = null; form.value.pageSize = null }
const paging = computed(() => !!(form.value.pageNo && form.value.pageSize))
</script>

<template>
  <div>
    <div class="field">
      <div class="fixed-table">
        <span class="fixed-table-label">数据表：</span>
        <span class="fixed-table-name">{{ FIXED_TABLE }}</span>
      </div>
    </div>

    <div class="field optional-section">
      <label>数据筛选 <span class="optional">可选</span><span v-if="condCount" class="tag mute">已添加 {{ condCount }} 条</span></label>
      <button v-if="!openCond" class="btn btn-block" @click="openCond = true">添加筛选条件</button>
      <div v-else>
        <div class="hint info-hint">例如：只看某家客户、某个大区、或某条状态的数据。</div>
        <div v-for="t in condTables" :key="t" class="cond-box">
          <div class="cond-head">
            <span class="ds-title">数据表：{{ t }}</span>
            <div class="topbar-spacer"></div>
            <button class="btn" style="padding:2px 8px;font-size:11px" @click="addRow(t)">+ 添加条件</button>
          </div>
          <div v-for="(row, ri) in rowsOf(t)" :key="ri" class="cond-row">
            <select v-model="row.field" :aria-label="`第 ${ri + 1} 条筛选的字段`"
                    @change="resetValue(t, row)"
                    :style="{ borderColor: fieldOk(row.field) ? '' : 'var(--err)' }">
              <option value="">请选择字段</option>
              <option v-for="field in fieldsOf(t)" :key="field.name" :value="field.name">
                {{ field.comment || field.name }}（{{ field.name }}）
              </option>
            </select>
            <select v-model="row.value" :aria-label="`第 ${ri + 1} 条筛选内容`"
                    :disabled="!row.field || !valuesOf(t, row.field).length"
                    :style="{ borderColor: row.value ? '' : 'var(--err)' }">
              <option value="">{{ row.field ? '请选择筛选内容' : '请先选择字段' }}</option>
              <option v-for="value in valuesOf(t, row.field)" :key="value" :value="value">{{ value }}</option>
            </select>
            <button class="btn" style="padding:2px 8px;color:var(--err)" @click="delRow(t, ri)">删</button>
          </div>
          <div v-if="condInvalid(t)" class="hint" style="color:var(--err);margin-top:5px">
            {{ fieldsOf(t).length ? '请选择字段和对应的筛选内容。' : '当前数据表暂无字段列表，请先获取一次结果后再配置筛选。' }}
          </div>
        </div>
        <div style="display:flex;gap:7px;margin-top:9px">
          <button class="btn" style="flex:1" @click="openCond = false">收起筛选</button>
          <button v-if="condCount" class="btn" style="flex:1" @click="clearCond">清空筛选</button>
        </div>
      </div>
      <div class="hint">不添加筛选时，系统会按权限返回全部可见数据。</div>
    </div>

    <div class="field optional-section">
      <label>数据量设置 <span class="optional">可选</span></label>
      <div class="row2">
        <input type="number" v-model="form.pageNo" placeholder="第几页，例如 1" min="1" @blur="clampPage" />
        <input type="number" v-model="form.pageSize" placeholder="每页多少条" min="1" :max="PAGE_MAX" @blur="clampPage" />
      </div>
      <div class="hint">不填写时使用系统默认数据量。需要大量数据时，可填写页码和每页条数（最多 10000 条）。
        <a v-if="paging" href="#" @click.prevent="clearPage">清除设置</a>
      </div>
    </div>

    <button class="btn btn-primary btn-block" @click="emit('submit')" :disabled="busy">
      <span v-if="busy" class="spin"></span>
      {{ busy ? '正在获取数据…' : '按此条件重新获取' }}
    </button>
    <button class="btn btn-block sample-button" style="margin-top:8px" @click="emit('sample')" :disabled="busy">
      先看一个示例结果（非实时数据）
    </button>
    <div v-if="!condValid && condCount" class="hint" style="color:var(--err);margin-top:7px">
      筛选条件尚未填写完整，提交时服务端会提示具体问题。
    </div>
  </div>
</template>
