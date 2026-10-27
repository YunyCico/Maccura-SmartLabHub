// 验收：进入平台自动拉取 + 业务化结果页
import { spawn } from 'node:child_process'
import { writeFileSync, mkdirSync } from 'node:fs'
import { setTimeout as sleep } from 'node:timers/promises'

const CHROME = process.env.LOCALAPPDATA + '\\Google\\Chrome\\Application\\chrome.exe'
const OUT = process.argv[2] || 'shots'
const PORT = 9336
const URL = 'http://127.0.0.1:8100/'

mkdirSync(OUT, { recursive: true })
const chrome = spawn(CHROME, [
  '--headless=new', `--remote-debugging-port=${PORT}`,
  '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  '--no-proxy-server', '--proxy-bypass-list=*',
  '--window-size=1600,1080', '--user-data-dir=' + process.env.TEMP + '\\dfp-cdp3',
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

async function shot(name, full = false) {
  let params = { format: 'png' }
  if (full) {
    const m = await send('Page.getLayoutMetrics')
    const c = m.cssContentSize || m.contentSize
    params = { ...params, captureBeyondViewport: true,
      clip: { x: 0, y: 0, width: Math.ceil(c.width),
              height: Math.min(Math.ceil(c.height), 4200), scale: 1 } }
  }
  const r = await send('Page.captureScreenshot', params)
  writeFileSync(`${OUT}/${name}.png`, Buffer.from(r.data, 'base64'))
  console.log(`  saved ${OUT}/${name}.png`)
}

await send('Page.enable'); await send('Runtime.enable')
await send('Emulation.setDeviceMetricsOverride',
  { width: 1600, height: 1080, deviceScaleFactor: 1, mobile: false })
await send('Page.navigate', { url: URL })

// 等待自动拉取完成（首屏 + 拉取 + 解析）
let autoOk = false
for (let i = 0; i < 40; i++) {
  await sleep(1000)
  const s = await evalJs(`(() => ({
    banner: document.querySelector('.hero-result-title')?.textContent?.trim() || null,
    err: document.querySelector('.alert-err')?.textContent?.trim() || null,
    busy: Boolean(document.querySelector('.empty .spin'))
  }))()`)
  if (s?.banner || s?.err) { autoOk = true; console.log('  auto pull state:', JSON.stringify(s)); break }
}
console.log('  auto pull triggered:', autoOk)
await sleep(800)
await shot('01-自动拉取结果')

console.log('  business layout:', JSON.stringify(await evalJs(`(() => {
  const kpiLabels = [...document.querySelectorAll('.kpi-label')].map(x => x.textContent.trim());
  const kpiValues = [...document.querySelectorAll('.kpi-value')].map(x => x.textContent.trim());
  const distTitles = [...document.querySelectorAll('.dist-title')].map(x => x.textContent.trim());
  const distRows = document.querySelectorAll('.dist-row').length;
  const tableHead = [...document.querySelectorAll('.panel-head h2')].map(x => x.textContent.trim());
  const firstColHead = document.querySelector('.tbl-wrap thead th:nth-child(2)')?.textContent?.trim() || null;
  return { kpiLabels, kpiValues, distTitles, distRows, tableHead, firstColHead };
})()`)))

// 明细搜索
console.log('  search:', await evalJs(`(() => {
  const inp = document.querySelector('.inline-search');
  if (!inp) return 'no-input';
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  setter.call(inp, '柳林县人民医院');
  inp.dispatchEvent(new Event('input', { bubbles: true }));
  return true;
})()`))
await sleep(700)
console.log('  filtered count:', await evalJs(
  `document.querySelector('.panel-head-actions .sub')?.textContent?.trim() || null`))
await shot('02-明细搜索')

// 分页
console.log('  pager:', await evalJs(`(() => {
  const p = document.querySelector('.pager-info');
  return p ? p.textContent.trim() : 'no-pager';
})()`))

// 展开技术信息
await evalJs(`(() => {
  const d = document.querySelector('details.tech-panel');
  if (d) { d.open = true; return true } return false
})()`)
await sleep(500)
await shot('03-技术信息展开')

await evalJs(`(() => { window.scrollTo(0,0); const s = document.querySelector('.stage'); if (s) s.scrollTop = 0; return true })()`)
await sleep(300)
await shot('04-完整页面', true)

console.log('  js errors:', JSON.stringify(errors.filter(Boolean)))
ws.close(); cleanup(); process.exit(0)
