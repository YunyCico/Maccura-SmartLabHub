// 验收：BI 看板页 —— 数据整合、图表渲染、筛选联动、下载可用
import { spawn } from 'node:child_process'
import { writeFileSync, mkdirSync } from 'node:fs'
import { setTimeout as sleep } from 'node:timers/promises'

const CHROME = process.env.LOCALAPPDATA + '\\Google\\Chrome\\Application\\chrome.exe'
const OUT = process.argv[2] || 'shots'
const PORT = 9339
const URL = 'http://127.0.0.1:8100/'

mkdirSync(OUT, { recursive: true })
const chrome = spawn(CHROME, [
  '--headless=new', `--remote-debugging-port=${PORT}`,
  '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  '--no-proxy-server', '--proxy-bypass-list=*',
  '--window-size=1600,1200', '--user-data-dir=' + process.env.TEMP + '\\dfp-cdp-bi',
  'about:blank'
], { stdio: 'ignore' })

const cleanup = () => { try { chrome.kill() } catch {} }
process.on('exit', cleanup)

async function target() {
  for (let i = 0; i < 40; i++) {
    try {
      const j = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json()
      const p = j.find(t => t.type === 'page')
      if (p?.webSocketDebuggerUrl) return p.webSocketDebuggerUrl
    } catch {}
    await sleep(500)
  }
  throw new Error('Chrome 调试端口未就绪')
}

const ws = new WebSocket(await target())
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej })

let id = 0
const pending = new Map()
const errors = []
ws.onmessage = (e) => {
  const m = JSON.parse(e.data)
  if (m.method === 'Runtime.exceptionThrown') errors.push(m.params?.exceptionDetails?.text)
  if (m.id && pending.has(m.id)) {
    const { res, rej } = pending.get(m.id); pending.delete(m.id)
    m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result)
  }
}
const send = (method, params = {}) => new Promise((res, rej) => {
  const mid = ++id; pending.set(mid, { res, rej })
  ws.send(JSON.stringify({ id: mid, method, params }))
})
const evalJs = async (expr) =>
  (await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true })).result.value

async function shot(name) {
  const r = await send('Page.captureScreenshot', { format: 'png' })
  writeFileSync(`${OUT}/${name}.png`, Buffer.from(r.data, 'base64'))
  console.log('shot ->', `${OUT}/${name}.png`)
}

await send('Page.enable')
await send('Runtime.enable')
await send('Page.navigate', { url: URL })
await sleep(9000)

// 滚动到 BI 看板面板（位于「字段说明」下方）
await evalJs(`document.querySelector('.stage').scrollTop = document.getElementById('bi-dashboard').offsetTop - 24`)
await sleep(11000)

const state = await evalJs(`(async () => {
  const f = document.querySelector('.bi-frame')
  const doc = f && f.contentDocument
  const d = doc ? doc.defaultView : null
  return {
    frameExists: !!f,
    navLabels: [...document.querySelectorAll('.nav-links a')].map(a => a.textContent.trim()),
    biPanelTitle: document.querySelector('#bi-dashboard h2')?.textContent,
    prevPanelTitle: document.querySelector('#bi-dashboard')?.previousElementSibling?.querySelector('h2')?.textContent,
    kpi: doc ? ['kpiTotal','kpiResponse','kpiEscalation'].map(id => doc.getElementById(id)?.textContent) : null,
    dataCount: doc ? doc.getElementById('dataCount')?.textContent : null,
    updateTime: doc ? doc.getElementById('updateTime')?.textContent : null,
    echartsLoaded: !!(d && d.echarts),
    chartCanvasCount: doc ? doc.querySelectorAll('canvas').length : 0,
    quickCards: doc ? doc.querySelectorAll('#quickRow .ins-card').length : 0,
    diagRows: doc ? doc.querySelectorAll('#diagTable tr').length : 0,
    dqItems: doc ? doc.querySelectorAll('#dqList > *').length : 0,
    availCardExists: doc ? !!doc.getElementById('availCard') : null,
    cardTitles: doc ? [...doc.querySelectorAll('#insBody .ins-card-hd h3')].map(h => h.textContent) : [],
    diffItems: doc ? doc.querySelectorAll('#diffList > *').length : 0,
    detailRows: doc ? doc.querySelectorAll('#detailTable tbody tr').length : 0,
    allDataLen: d ? (d.allData ? d.allData.length : -1) : -2,
    barActions: [...document.querySelectorAll('.bi-bar-actions a,.bi-bar-actions button')].map(e => e.textContent.trim()),
    biTip: document.querySelector('.bi-tip')?.textContent?.trim().slice(0, 60),

  }
})()`)
console.log('STATE', JSON.stringify(state, null, 2))

// 字段说明：默认折叠 → 点击标题栏展开 → 再点收起
const fold = await evalJs(`(async () => {
  const panel = document.querySelector('#bi-dashboard')?.previousElementSibling
  const head = panel?.querySelector('.panel-head-toggle')
  if (!panel || !head) return { skipped: true }
  const body = panel.querySelector('.panel-body')
  const hint = head.querySelector('.toggle-hint')?.textContent.trim()
  const sleep = ms => new Promise(r => setTimeout(r, ms))
  const res = { initialOpen: panel.classList.contains('open'), initialHint: hint,
                initialBodyVisible: body.offsetHeight > 0 }
  head.click(); await sleep(350)
  res.afterExpandOpen = panel.classList.contains('open')
  res.afterExpandHint = head.querySelector('.toggle-hint')?.textContent.trim()
  res.afterExpandRows = body.querySelectorAll('tbody tr').length
  res.afterExpandBodyVisible = body.offsetHeight > 0
  head.click(); await sleep(350)
  res.afterCollapseOpen = panel.classList.contains('open')
  res.afterCollapseBodyVisible = body.offsetHeight > 0
  head.click(); await sleep(350)
  return res
})()`)
console.log('FOLD', JSON.stringify(fold))
await shot('bi-04-fieldspec-open')

// BI 看板：默认展开 → 折叠 → 顶栏导航应自动展开并滚到位
const biFold = await evalJs(`(async () => {
  const p = document.getElementById('bi-dashboard')
  const head = p.querySelector('.panel-head-toggle')
  const body = p.querySelector('.panel-body')
  const sleep = ms => new Promise(r => setTimeout(r, ms))
  const res = { initialOpen: p.classList.contains('open'),
                initialHint: head.querySelector('.toggle-hint').textContent.trim(),
                initialFrameVisible: body.offsetHeight > 0 }
  head.click(); await sleep(300)
  res.afterCollapseOpen = p.classList.contains('open')
  res.afterCollapseFrameVisible = body.offsetHeight > 0
  // 折叠态下点顶栏「BI 看板」：应自动展开并滚到位
  const nav = [...document.querySelectorAll('.nav-links a')].find(a => a.textContent.includes('BI'))
  nav.click(); await sleep(700)
  const stage = document.querySelector('.stage')
  res.navReopened = p.classList.contains('open')
  res.scrollTop = stage.scrollTop
  res.panelTop = p.offsetTop
  res.reached = Math.abs(stage.scrollTop - (p.offsetTop - 24)) < 40
  res.canvasAfterNav = document.querySelector('.bi-frame').contentDocument.querySelectorAll('canvas').length
  return res
})()`)
console.log('BIFOLD', JSON.stringify(biFold))
await shot('bi-05-bi-folded')

await shot('bi-01-view')
// 截字段说明 → 看板的衔接处
await evalJs(`document.querySelector('.stage').scrollTop = document.getElementById('bi-dashboard').offsetTop - 620`)
await sleep(1500)
await shot('bi-03-boundary')

// 筛选联动：选大区
const filtered = await evalJs(`(() => {
  const doc = document.querySelector('.bi-frame').contentDocument
  const sel = doc.getElementById('filterRegion')
  const opt = [...sel.options].find(o => o.value)
  if (!opt) return { skipped: true, options: [...sel.options].map(o => o.value) }
  sel.value = opt.value
  doc.defaultView.renderAll()
  return {
    picked: opt.value,
    kpiTotal: doc.getElementById('kpiTotal').textContent,
    insScope: doc.getElementById('insScope').textContent,
    chartCanvas: doc.querySelectorAll('canvas').length
  }
})()`)
console.log('FILTER', JSON.stringify(filtered))
await sleep(1500)
await shot('bi-02-filtered')

// 下载可用性（真实发起 HTTP GET，取回并检查头）
const dl = await evalJs(`(async () => {
  const out = {}
  for (const [k, u] of [['xlsx','/api/bi/export.xlsx'],['html','/api/bi/export.html']]) {
    const r = await fetch(u)
    const b = await r.blob()
    out[k] = { status: r.status, size: b.size, cd: r.headers.get('content-disposition') }
  }
  return out
})()`)
console.log('DOWNLOAD', JSON.stringify(dl, null, 2))

// 导航跳转测试
console.log('NAV', JSON.stringify(await evalJs(`(() => {
  const stage = document.querySelector('.stage')
  const links = [...document.querySelectorAll('.nav-links a')]
  links[1].click()
  const after = stage.scrollTop
  links[0].click()
  return { biScrollTop: Math.round(after), backToTop: Math.round(stage.scrollTop) }
})()`)))
console.log('JS_ERRORS', JSON.stringify(errors))
ws.close()
cleanup()
process.exit(0)