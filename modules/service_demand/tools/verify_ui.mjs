// 用 Chrome CDP 截图验收控制台页面
import { spawn } from 'node:child_process'
import { writeFileSync, mkdirSync } from 'node:fs'
import { setTimeout as sleep } from 'node:timers/promises'

const CHROME = process.env.LOCALAPPDATA + '\\Google\\Chrome\\Application\\chrome.exe'
const OUT = process.argv[2] || 'shots'
const PORT = 9334
const URL = 'http://127.0.0.1:8100/'

mkdirSync(OUT, { recursive: true })
const chrome = spawn(CHROME, [
  '--headless=new', `--remote-debugging-port=${PORT}`,
  '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  '--window-size=1600,1080', '--user-data-dir=' + process.env.TEMP + '\\dfp-cdp2',
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

const clickByText = (sel, text) => evalJs(`(() => {
  const b = [...document.querySelectorAll('${sel}')]
    .find(x => x.textContent.includes(${JSON.stringify(text)}));
  if (b) { b.click(); return true } return false
})()`)

const readFormState = () => evalJs(`(() => {
  const row = document.querySelector('.cond-row');
  return {
    guideVisible: Boolean(document.querySelector('.guide-card')),
    fixedTable: document.querySelector('.fixed-table-name')?.textContent.trim() || null,
    tablePickerPresent: Boolean(document.querySelector('.chip-scroll')),
    selectCount: row ? row.querySelectorAll('select').length : 0,
    inputCount: row ? row.querySelectorAll('input').length : 0,
    valueOptions: row ? [...row.querySelectorAll('select')[1]?.options || []].map(x => x.value) : []
  };
})()` )

await send('Page.enable'); await send('Runtime.enable')
await send('Emulation.setDeviceMetricsOverride',
  { width: 1600, height: 1080, deviceScaleFactor: 1, mobile: false })
await send('Page.navigate', { url: URL })
await sleep(5000)
await shot('01-初始页')

// 固定数据表展示
console.log('  form state:', JSON.stringify(await readFormState()))

// 打开 condition 编辑器并加一行
console.log('  open cond:', await clickByText('button', '添加筛选条件'))
await sleep(700)
console.log('  add cond row:', await clickByText('button', '+ 添加条件'))
await sleep(400)
console.log('  field debug:', await evalJs(`(async () => {
  const meta = await (await fetch('/api/meta')).json();
  const s = document.querySelector('.cond-row select');
  const box = document.querySelector('.cond-box');
  return {
    metaFields: Object.keys(meta.data.tableFields || {}),
    defaultFields: (meta.data.tableFields?.ADS_SERVICE_DEMAND_FEEDBACK || []).length,
    boxText: box?.textContent?.slice(0, 120),
    options: s ? [...s.options].map(x => x.textContent.trim()) : []
  };
})()`))
// 选择字段并填写筛选值
console.log('  select field:', await evalJs(`(() => {
  const select = document.querySelector('.cond-row select');
  if (!select) return false;
  select.value = 'ISUPGRADE';
  select.dispatchEvent(new Event('change', { bubbles: true }));
  return true;
})()`))
await sleep(500)
console.log('  linked values:', await evalJs(`(() => {
  const selects = document.querySelectorAll('.cond-row select');
  if (selects.length < 2) return [];
  return [...selects[1].options].map((option) => option.value);
})()`))
console.log('  fill cond:', await evalJs(`(() => {
  const selects = document.querySelectorAll('.cond-row select');
  if (selects.length < 2) return false;
  selects[1].value = '是';
  selects[1].dispatchEvent(new Event('change', { bubbles: true }));
  return selects[1].value === '是';
})()`))
await sleep(500)
// 滚动侧栏到 condition 区域再截图
await evalJs(`(() => {
  const el = document.querySelector('.cond-box');
  if (el) { el.scrollIntoView({ block: 'center' }); return true }
  return false
})()`)
await sleep(600)
await shot('02-条件编辑器')

// 预览样本
console.log('  sample:', await clickByText('button', '先看一个示例结果'))
await sleep(3000)
await shot('03-解析结果')

// 展开第一张表
console.log('  expand:', await evalJs(`(() => {
  const h = document.querySelector('.ds-head'); if (h) { h.click(); return true } return false
})()`))
await sleep(1500)
await shot('04-字段画像', true)

// 供应商指标入口应已移除
console.log('  supplier tab removed:', await evalJs(`!document.body.innerText.includes('供应商指标')`))
await shot('05-固定数据表')

console.log('  js errors:', JSON.stringify(errors.filter(Boolean)))
ws.close(); cleanup(); process.exit(0)