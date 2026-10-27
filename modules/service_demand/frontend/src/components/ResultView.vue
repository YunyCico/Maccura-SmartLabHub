<script setup>
import { ref, computed, watch } from 'vue'
import { withBase } from '../api.js'

const props = defineProps({ result: Object, busy: Boolean, history: Array })
const emit = defineEmits(['refresh'])

const openDs = ref({})
const keyword = ref('')
const page = ref(1)
const PAGE_SIZE = 20

// BI 看板：改key 强制 iframe 重新加载
const biKey = ref(0)
// 字段说明默认折叠，展开状态在本次会话内保持
const fieldSpecOpen = ref(false)
// BI 看板默认展开（保持原样），可手动折叠；刷新看板时自动保持展开
const biOpen = ref(true)
function reloadBi() { biKey.value += 1 }

const ds = computed(() => props.result?.datasets || [])
const metrics = computed(() => props.result?.metrics || [])
const combos = computed(() => props.result?.combinations || [])
const manifest = computed(() => props.result?.manifest || {})
const hasData = computed(() =>
  ds.value.length + metrics.value.length + combos.value.length > 0
)

/* ---------- 表级业务画像 ---------- */
const table = computed(() => ds.value[0] || null)
const tableTitle = computed(() => {
  const t = table.value
  if (!t) return '数据表'
  return t.chineseName || t.name
})

/** 字段名 → 中文名，便于业务人员看懂的表头 */
const cnOf = (name) => {
  const f = (table.value?.fields || []).find((x) => x.name === name)
  return f?.comment || ''
}
const fieldCn = (name) => cnOf(name) || name

/* 按业务语义挑出关键字段（不同表用不同列名，靠中文名+字段名双重匹配） */
function pickFieldOf(keys, names) {
  const fields = table.value?.fields || []
  for (const k of keys) {
    const hit = fields.find((f) => f.comment === k || f.name === k)
    if (hit) return hit
  }
  for (const n of names) {
    const hit = fields.find((f) => f.name === n)
    if (hit) return hit
  }
  return null
}

const F_DATE = computed(() => pickFieldOf(['创建日期', '提交日期', '需求日期'], ['CREATEDATE']))
const F_CUSTOMER = computed(() => pickFieldOf(['终端客户', '客户名称', '客户'], ['ZDKH']))
const F_REGION = computed(() => pickFieldOf(['大区', '地区', '区域'], ['DQ']))
const F_CAT1 = computed(() => pickFieldOf(['一级分类'], ['YJFL']))
const F_CAT2 = computed(() => pickFieldOf(['二级分类'], ['EJFL']))
const F_STATUS = computed(() => pickFieldOf(['问题状态', '状态', '流程节点'], ['SELECTNAME5', 'NODENAME']))
const F_OWNER = computed(() => pickFieldOf(['运营专员', '负责人', '处理人'], ['LASTNAME1']))
const F_DEPT = computed(() => pickFieldOf(['解决部门（主要）', '解决部门', '所属部门', '处理部门'], ['DEPARTMENTNAME3', 'DEPARTMENTNAME1']))
const F_SATISFY = computed(() => pickFieldOf(['客户满意度', '满意度'], ['SELECTNAME7']))
const F_SOURCE = computed(() => pickFieldOf(['反馈来源'], ['FKLY']))
const F_RESPONSE = computed(() => pickFieldOf(['是否响应'], ['SELECTNAME2']))

/* ---------- 概览指标 ---------- */
const kpis = computed(() => {
  const t = table.value
  if (!t) return []
  const out = []
  out.push({ label: '需求单总数', value: t.rowCount.toLocaleString(), unit: '条', cls: 'blue' })
  const cust = F_CUSTOMER.value
  if (cust) {
    out.push({
      label: '涉及客户',
      value: cust.uniqueCount.toLocaleString(),
      unit: '家',
      cls: '',
      sub: cust.samples?.[0] ? `如 ${cust.samples[0]}` : ''
    })
  }
  const region = F_REGION.value
  if (region) {
    out.push({ label: '覆盖大区', value: region.uniqueCount.toLocaleString(), unit: '个', cls: '' })
  }
  const cat = F_CAT1.value
  if (cat) {
    out.push({ label: '需求分类', value: cat.uniqueCount.toLocaleString(), unit: '类', cls: '' })
  }
  const owner = F_OWNER.value
  if (owner) {
    out.push({ label: '处理人数', value: owner.uniqueCount.toLocaleString(), unit: '人', cls: '' })
  }
  const date = F_DATE.value
  if (date?.dateRange?.min) {
    out.push({
      label: '数据时间范围',
      value: date.dateRange.min.slice(5),
      unit: '',
      cls: '',
      sub: `至 ${date.dateRange.max.slice(5)}`
    })
  }
  return out
})

/* ---------- 分布分析（业务人员最常看的几个维度） ---------- */
function distOf(field, limit = 8) {
  if (!field) return []
  const d = field.dist || field.top || []
  return d.slice(0, limit).map((x) => ({
    ...x,
    label: x.value === '' ? '（空）' : x.value,
    pct: field.nonEmpty ? x.count / field.nonEmpty : 0
  }))
}

const distributions = computed(() => {
  const defs = [
    { title: '需求分类', field: F_CAT1.value, icon: '◧' },
    { title: '大区分布', field: F_REGION.value, icon: '◨' },
    { title: '处理状态', field: F_STATUS.value, icon: '◩' },
    { title: '客户满意度', field: F_SATISFY.value, icon: '◪' },
    { title: '反馈来源', field: F_SOURCE.value, icon: '◫' },
    { title: '处理部门', field: F_DEPT.value, icon: '◬' }
  ]
  return defs
    .map((d) => ({ ...d, items: distOf(d.field) }))
    .filter((d) => d.items.length > 1)
})

/* ---------- 明细表：搜索 + 分页 ---------- */
const columns = computed(() => table.value?.columns || [])
const allRows = computed(() => table.value?.preview || [])

const filteredRows = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  if (!kw) return allRows.value
  return allRows.value.filter((r) =>
    columns.value.some((c) => String(r[c] ?? '').toLowerCase().includes(kw))
  )
})
const totalPages = computed(() => Math.max(1, Math.ceil(filteredRows.value.length / PAGE_SIZE)))
const pagedRows = computed(() =>
  filteredRows.value.slice((page.value - 1) * PAGE_SIZE, page.value * PAGE_SIZE)
)
watch([keyword, () => props.result], () => { page.value = 1 })

function isNum(v) {
  return v !== '' && v !== null && !isNaN(Number(v))
}
function toggle(i) { openDs.value[i] = !openDs.value[i] }
function fieldOf(d, name) { return (d.fields || []).find((f) => f.name === name) || {} }

const isSample = computed(() => (props.result?.fileName || '').includes('本地样本'))
/* 0 条告警：必须「确实一行都没拿到」才提示，避免 manifest 文案误伤 */
const zeroRows = computed(() => {
  if (isSample.value) return 0
  const totalRows = ds.value.reduce((s, d) => s + (d.rowCount || 0), 0)
  if (totalRows > 0) return 0
  const m = manifest.value.message || ''
  return (m.match(/成功返回\s*0/g) || []).length
})
const pagingInfo = computed(() =>
  (manifest.value.tables || []).map((t) => ({
    name: t.tableName, cn: t.chineseName || '',
    rowCount: t.rowCount, total: t.total,
    totalPages: t.totalPages, pageNo: t.pageNo, pageSize: t.pageSize
  }))
)
</script>

<template>
  <div v-if="busy" class="panel">
    <div class="panel-body">
      <div class="empty">
        <div class="empty-icon"><span class="spin"></span></div>
        正在自动获取「服务需求反馈」数据…<br />
        登录中台 → 下载文件 → 解析内容
      </div>
    </div>
  </div>

  <div v-else-if="!result" class="panel">
    <div class="panel-body">
      <div class="empty">
        <div class="empty-icon">◈</div>
        正在准备数据，稍候即可看到结果
      </div>
    </div>
  </div>

  <div v-else-if="result.error" class="panel">
    <div class="panel-body">
      <div class="alert alert-err">{{ result.error }}</div>
      <button class="btn btn-primary" @click="emit('refresh')">重新获取数据</button>
    </div>
  </div>

  <div v-else>
    <!-- 成功横幅 -->
    <div class="hero-result">
      <div class="hero-result-left">
        <div class="hero-result-badge">✓</div>
        <div>
          <h2 class="hero-result-title">数据获取成功</h2>
          <p class="hero-result-sub">
            {{ tableTitle }} · 共 <b>{{ (table?.rowCount ?? 0).toLocaleString() }}</b> 条记录
            · {{ (table?.columnCount ?? 0) }} 个字段
            <span v-if="result.elapsedSec != null"> · 耗时 {{ result.elapsedSec }} 秒</span>
          </p>
        </div>
      </div>
      <button class="btn btn-primary" @click="emit('refresh')">刷新数据</button>
    </div>

    <div v-if="isSample" class="alert alert-info sample-notice">
      <strong>当前显示的是演示数据，不是数据中台实时数据。</strong><br />
      要核对真实数据，请点击右上角「刷新数据」重新获取。
    </div>

    <div v-if="zeroRows > 0" class="alert alert-warn">
      <strong>接口调用成功，但所有资源均返回 0 条。</strong><br />
      请求链路没有问题，是当前授权账号的<strong>数据权限范围</strong>
      未覆盖这些表或指标。请联系数据中台管理员开通对应业务分类的查看权限。
    </div>

    <!-- 业务概览 -->
    <div v-if="kpis.length" class="kpis">
      <div v-for="k in kpis" :key="k.label" class="kpi" :class="k.cls">
        <div class="kpi-label">{{ k.label }}</div>
        <div class="kpi-value">{{ k.value }}<span v-if="k.unit" class="kpi-unit">{{ k.unit }}</span></div>
        <div v-if="k.sub" class="kpi-sub">{{ k.sub }}</div>
      </div>
    </div>

    <!-- 分布分析 -->
    <div v-if="distributions.length" class="panel">
      <div class="panel-head">
        <h2>业务分布</h2>
        <span class="sub">按常见维度统计，一眼看清数据构成</span>
      </div>
      <div class="panel-body">
        <div class="dist-grid">
          <div v-for="d in distributions" :key="d.title" class="dist-card">
            <div class="dist-title">{{ d.title }}</div>
            <div class="dist-list">
              <div v-for="it in d.items" :key="it.label" class="dist-row">
                <div class="dist-label" :title="it.label">{{ it.label }}</div>
                <div class="dist-bar-wrap">
                  <div class="dist-bar" :style="{ width: Math.max(2, it.pct * 100) + '%' }"></div>
                </div>
                <div class="dist-count">{{ it.count }}</div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 明细数据 -->
    <div v-if="table" class="panel">
      <div class="panel-head">
        <h2>需求明细</h2>
        <span class="sub">{{ tableTitle }}</span>
        <div class="panel-head-actions">
          <input
            v-model="keyword"
            class="inline-search"
            type="text"
            placeholder="搜索任意内容…"
            aria-label="搜索明细" />
          <span class="sub">{{ filteredRows.length }} 条</span>
        </div>
      </div>
      <div class="panel-body">
        <div class="tbl-wrap">
          <table>
            <thead>
              <tr>
                <th style="width:44px">#</th>
                <th v-for="c in columns" :key="c">
                  {{ fieldCn(c) }}
                  <div class="comment-cell">{{ c }}</div>
                </th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(row, ri) in pagedRows" :key="ri">
                <td style="color:var(--text-3)">{{ (page - 1) * PAGE_SIZE + ri + 1 }}</td>
                <td v-for="c in columns" :key="c"
                    :class="{ num: isNum(row[c]) }" :title="String(row[c] ?? '')">{{ row[c] }}</td>
              </tr>
              <tr v-if="!pagedRows.length">
                <td :colspan="columns.length + 1" style="text-align:center;color:var(--text-3)">
                  没有匹配「{{ keyword }}」的记录
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-if="totalPages > 1" class="pager">
          <button class="btn" :disabled="page <= 1" @click="page--">上一页</button>
          <span class="pager-info">第 {{ page }} / {{ totalPages }} 页</span>
          <button class="btn" :disabled="page >= totalPages" @click="page++">下一页</button>
        </div>
      </div>
    </div>

    <!-- 字段说明（业务口径） · 默认折叠，点击标题栏展开 -->
    <div v-if="table" class="panel panel-collapsible" :class="{ open: fieldSpecOpen }">
      <div
        class="panel-head panel-head-toggle"
        role="button"
        tabindex="0"
        :aria-expanded="fieldSpecOpen"
        @click="fieldSpecOpen = !fieldSpecOpen"
        @keydown.enter.prevent="fieldSpecOpen = !fieldSpecOpen"
        @keydown.space.prevent="fieldSpecOpen = !fieldSpecOpen"
      >
        <span class="chev" :class="{ turn: fieldSpecOpen }" aria-hidden="true"></span>
        <h2>字段说明</h2>
        <span class="sub">每个字段的业务含义与数据完整度（{{ table.fields.length }} 个字段）</span>
        <span class="toggle-hint">{{ fieldSpecOpen ? '收起' : '展开' }}</span>
      </div>
      <div v-show="fieldSpecOpen" class="panel-body">
        <div class="tbl-wrap" style="max-height:420px">
          <table>
            <thead>
              <tr>
                <th>业务名称</th>
                <th>字段名</th>
                <th>类型</th>
                <th>完整度</th>
                <th>去重值</th>
                <th>常见取值</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="f in table.fields" :key="f.name">
                <td>{{ f.comment || '—' }}</td>
                <td style="font-family:var(--mono);font-size:11.5px">{{ f.name }}</td>
                <td><span class="type-tag">{{ f.fieldType || '—' }}</span></td>
                <td>
                  <div class="mini-bar-wrap">
                    <div class="mini-bar">
                      <div class="mini-bar-fill" :style="{ width: (f.nonEmptyRate * 100) + '%' }"></div>
                    </div>
                    <span class="num">{{ (f.nonEmptyRate * 100).toFixed(0) }}%</span>
                  </div>
                </td>
                <td class="num">{{ f.uniqueCount }}</td>
                <td class="sample-cell">
                  <span v-for="s in (f.top || []).slice(0, 3)" :key="s.value" class="tag mute">{{ s.value }}</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- BI 看板（服务质量需求分析） · 可折叠 -->
    <div class="panel panel-collapsible" id="bi-dashboard" :class="{ open: biOpen }">
      <div
        class="panel-head panel-head-toggle"
        role="button"
        tabindex="0"
        :aria-expanded="biOpen"
        @click="biOpen = !biOpen"
        @keydown.enter.prevent="biOpen = !biOpen"
        @keydown.space.prevent="biOpen = !biOpen"
      >
        <span class="chev" :class="{ turn: biOpen }" aria-hidden="true"></span>
        <h2>服务质量需求 BI 看板</h2>
        <span class="sub">数据来自本次拉取结果，图表随筛选联动</span>
        <span class="toggle-hint">{{ biOpen ? '收起' : '展开' }}</span>
      </div>
      <div v-show="biOpen" class="panel-body">
        <div class="bi-bar">
          <div class="bi-bar-text">
            <strong>服务质量需求 BI 看板</strong>
            <span>共 5 张 KPI 卡、15 张分析图表、关键指标解读与数据质量提示</span>
          </div>
          <div class="bi-bar-actions">
            <button class="btn" @click="reloadBi">刷新看板</button>
            <a class="btn" :href="withBase('/api/bi/mapping.xlsx')" download title="下载后可修改字段对应关系，改完刷新看板即生效">字段对应表</a>
            <a class="btn" :href="withBase('/api/bi/export.xlsx')" download>下载 Excel</a>
            <a class="btn btn-primary" :href="withBase('/api/bi/export.html')" download>下载看板 HTML</a>
          </div>
        </div>
        <p class="bi-tip">
          字段对应关系由「字段对应表」决定：在上表里把「取数字段」改成真实表字段名即可，
          改完点「刷新看板」即可看到新报表；「候选字段清单」页列出了全部 66 个可选字段。
        </p>
        <iframe class="bi-frame" :key="biKey" :src="withBase('/bi-dashboard-template.html')" title="服务质量需求 BI 看板"></iframe>
      </div>
    </div>

    <!-- 指标与组合 -->
    <div v-if="metrics.length || combos.length" class="panel">
      <div class="panel-head">
        <h2>指标与组合结果</h2>
        <span class="sub">{{ metrics.length }} 指标 / {{ combos.length }} 组合</span>
      </div>
      <div class="panel-body">
        <div v-for="m in metrics" :key="'m'+m.code" class="ds">
          <div class="ds-head">
            <span class="tag">指标</span>
            <div style="flex:1">
              <div class="ds-title">{{ m.code }}</div>
              <div class="ds-meta">{{ m.table.rows.length }} 条记录 · {{ m.path }}</div>
            </div>
          </div>
          <div class="ds-body">
            <div class="tbl-wrap">
              <table>
                <thead><tr><th v-for="c in m.table.columns" :key="c">{{ c }}</th></tr></thead>
                <tbody>
                  <tr v-for="(row, ri) in m.table.rows.slice(0, 200)" :key="ri">
                    <td v-for="c in m.table.columns" :key="c"
                        :class="{ num: isNum(row[c]) }">{{ row[c] }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
        <div v-for="c in combos" :key="'c'+c.code" class="ds">
          <div class="ds-head">
            <span class="tag">组合</span>
            <div style="flex:1">
              <div class="ds-title">{{ c.code }}</div>
              <div class="ds-meta">{{ c.table.rows.length }} 条记录 · {{ c.path }}</div>
            </div>
          </div>
          <div class="ds-body">
            <div class="tbl-wrap">
              <table>
                <thead><tr><th v-for="cc in c.table.columns" :key="cc">{{ cc }}</th></tr></thead>
                <tbody>
                  <tr v-for="(row, ri) in c.table.rows.slice(0, 200)" :key="ri">
                    <td v-for="cc in c.table.columns" :key="cc"
                        :class="{ num: isNum(row[cc]) }">{{ row[cc] }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 技术细节（折叠，业务人员默认不关心） -->
    <details class="panel tech-panel">
      <summary class="panel-head" style="cursor:pointer;list-style:none">
        <h2>技术信息</h2>
        <span class="sub">文件信息 · 接口回执 · 下载链接</span>
      </summary>
      <div class="panel-body">
        <div class="kv">
          <span class="kv-k">文件名称</span>
          <span class="kv-v">{{ result.fileName }}</span>
        </div>
        <div v-if="result.downloadUrl" class="kv">
          <span class="kv-k">临时下载地址</span>
          <span class="kv-v"><a :href="result.downloadUrl" target="_blank">{{ result.downloadUrl }}</a></span>
        </div>
        <div class="kv">
          <span class="kv-k">本机保存位置</span>
          <span class="kv-v" style="font-size:11px">{{ result.savedDir }}</span>
        </div>
        <div v-if="manifest.message" class="kv">
          <span class="kv-k">接口回执</span>
          <span class="kv-v">{{ manifest.message }}</span>
        </div>
        <div v-if="pagingInfo.length" style="margin-top:10px">
          <div v-for="t in pagingInfo" :key="t.name" class="kv">
            <span class="kv-k">{{ t.name }}</span>
            <span class="kv-v">
              {{ t.cn || '—' }}
              <template v-if="t.total !== undefined && t.total !== null">
                · 本次 {{ t.rowCount }} / 可见总量 {{ t.total }} · 第 {{ t.pageNo }} 页
                （共 {{ t.totalPages }} 页，每页 {{ t.pageSize }}）
              </template>
            </span>
          </div>
        </div>
        <details v-if="(result.entries || []).length" style="margin-top:10px">
          <summary class="tech-summary">ZIP 内全部条目（{{ result.entries.length }} 个）</summary>
          <div v-for="e in result.entries" :key="e.name" class="kv">
            <span class="kv-k">{{ e.size }} B</span>
            <span class="kv-v">{{ e.name }}</span>
            <span class="tag mute" style="margin-left:auto">{{ e.kind }}</span>
          </div>
        </details>
      </div>
    </details>

    <div v-if="(result.errors || []).length">
      <div v-for="(e, i) in result.errors" :key="i" class="alert alert-warn">{{ e }}</div>
    </div>

    <div v-if="history.length" class="panel">
      <div class="panel-head">
        <h2>历史拉取</h2>
        <span class="sub">最近 {{ history.length }} 次</span>
      </div>
      <div class="panel-body">
        <div class="tbl-wrap" style="max-height:240px">
          <table>
            <thead><tr><th>时间</th><th>文件</th><th>表数</th><th>总行数</th><th>大小</th></tr></thead>
            <tbody>
              <tr v-for="h in history" :key="h.id">
                <td>{{ h.time }}</td>
                <td style="font-family:var(--mono)">{{ h.fileName }}</td>
                <td class="num">{{ h.datasetCount }}</td>
                <td class="num">{{ (h.totalRows || 0).toLocaleString() }}</td>
                <td class="num">{{ (h.zipSize / 1024).toFixed(1) }} KB</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
</template>
