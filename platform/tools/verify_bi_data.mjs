/**
 * 验证：「服务需求报表」里的 BI 看板能真正取到数据（不再显示"后端暂无数据"）。
 *
 * 为什么单独测这个（2026-10-27）：
 * 看板模板 bi-dashboard-template.html 里有一处 fetch('/api/bi/dataset') 是**绝对路径**，
 * 绕过了 withBase()。当模块被平台嵌在 /portal/ 下时，浏览器会把它解析成
 *   http://<host>/api/bi/dataset
 * 打到了**平台后端**上 —— 平台根本没有这个路由（它只存在于 portal 子应用里），
 * 于是拿不到行数据，看板显示「后端暂无数据，请先在数据拉取页获取数据」。
 *
 * 这个脚本做真实浏览器验证：
 *   1. 登录平台
 *   2. 打开服务需求报表页
 *   3. 进入 iframe 内的 BI 看板，断言：
 *      - iframe 里的 fetch 目标带 /portal 前缀
 *      - /portal/api/bi/dataset 真的返回 JSON 且 rows 非空
 *      - 看板上的统计卡片不是 0（总需求数 > 0）
 *      - 没有出现"后端暂无数据"的 toast
 *
 * 用法：node platform/tools/verify_bi_data.mjs
 * 前置：平台已在跑（后端 8000 + Vite 5173）
 */
import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync } from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';

const CHROME = path.join(process.env.LOCALAPPDATA || '', 'Google/Chrome/Application/chrome.exe');
const FRONTEND = process.env.SLH_FRONTEND_URL || 'http://127.0.0.1:5173';
const USERNAME = process.env.SLH_USER || '95';
const PASSWORD = process.env.SLH_PASS || '123456000';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const results = [];
function check(name, ok, extra = '') {
  results.push({ name, ok, extra });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${extra ? '  ' + extra : ''}`);
}

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.listen(0, '127.0.0.1', () => {
      const address = server.address();
      server.close(() => resolve(address.port));
    });
    server.on('error', reject);
  });
}

async function getJson(url) {
  try {
    const response = await fetch(url, { signal: AbortSignal.timeout(3000) });
    return await response.json();
  } catch { return null; }
}

class Cdp {
  constructor(ws) {
    this.ws = ws; this.id = 0; this.pending = new Map(); this.contexts = new Map();
    ws.addEventListener('message', (event) => {
      const message = JSON.parse(event.data);
      if (message.id && this.pending.has(message.id)) {
        const pending = this.pending.get(message.id);
        this.pending.delete(message.id);
        pending.resolve(message);
        return;
      }
      if (message.method === 'Runtime.executionContextCreated') {
        this.contexts.set(message.params.context.id, message.params.context);
      }
      if (message.method === 'Runtime.executionContextDestroyed') {
        this.contexts.delete(message.params.executionContextId);
      }
      if (message.method === 'Runtime.executionContextsCleared') {
        this.contexts.clear();
      }
    });
  }
  send(method, params = {}, timeoutMs = 30000) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error(`timeout: ${method}`)); }, timeoutMs);
      this.pending.set(id, { resolve: (m) => { clearTimeout(timer); resolve(m); } });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  async evalJson(expression, contextId) {
    const params = { expression, returnByValue: true, awaitPromise: true };
    if (contextId) params.contextId = contextId;
    const response = await this.send('Runtime.evaluate', params);
    const value = response?.result?.result?.value;
    if (value === undefined) throw new Error('evaluate failed: ' + JSON.stringify(response).slice(0, 300));
    return typeof value === 'string' ? JSON.parse(value) : value;
  }
}

function openWs(url) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(url);
    ws.addEventListener('open', () => resolve(ws), { once: true });
    ws.addEventListener('error', () => reject(new Error('websocket error')), { once: true });
  });
}

async function main() {
  if (!existsSync(CHROME)) { console.log('SKIP chrome missing'); return 0; }

  const env = { ...process.env, NO_PROXY: '*', no_proxy: '*' };
  for (const key of Object.keys(env)) if (/proxy/i.test(key)) delete env[key];

  let chrome = null; let profile = null;
  try {
    const debugPort = await freePort();
    profile = mkdtempSync(path.join(os.tmpdir(), 'slh_bidata_'));
    chrome = spawn(CHROME, [
      `--remote-debugging-port=${debugPort}`, `--user-data-dir=${profile}`,
      '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
      '--no-proxy-server', '--proxy-bypass-list=*', '--window-size=1500,1100', 'about:blank',
    ], { env, stdio: 'ignore' });

    let version = null;
    const vEnd = Date.now() + 25000;
    while (Date.now() < vEnd && !version) {
      version = await getJson(`http://127.0.0.1:${debugPort}/json/version`);
      if (!version) await sleep(300);
    }
    if (!version) throw new Error('devtools unreachable');

    const browser = new Cdp(await openWs(version.webSocketDebuggerUrl));
    const created = await browser.send('Target.createTarget', { url: 'about:blank' });
    const tid = created.result.targetId;
    let info = null;
    const tEnd = Date.now() + 15000;
    while (Date.now() < tEnd && !info) {
      const list = await fetch(`http://127.0.0.1:${debugPort}/json/list`).then((r) => r.json()).catch(() => []);
      info = list.find((t) => t.id === tid);
      if (!info) await sleep(300);
    }
    const page = new Cdp(await openWs(info.webSocketDebuggerUrl));
    await page.send('Runtime.enable');
    await page.send('Page.enable');

    async function ctx(originHint) {
      for (let i = 0; i < 40; i++) {
        const c = [...page.contexts.values()].find((x) => x.origin.includes(originHint))?.id;
        if (c) return c;
        await sleep(300);
      }
      return null;
    }
    async function typeInto(selector, value) {
      await page.evalJson(`(function () {
        var el = document.querySelector('${selector}');
        var setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
        setter.call(el, ${JSON.stringify(value)});
        el.dispatchEvent(new Event('input', { bubbles: true }));
        return true;
      })()`, await ctx(':5173'));
    }
    async function click(selector) {
      const p = await page.evalJson(`(function () {
        var el = document.querySelector('${selector}');
        if (!el) return null;
        el.scrollIntoView({block:'center'});
        var r = el.getBoundingClientRect();
        return { x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2) };
      })()`, await ctx(':5173'));
      if (!p) throw new Error('找不到元素: ' + selector);
      await sleep(250);
      for (const type of ['mousePressed', 'mouseReleased']) {
        await page.send('Input.dispatchMouseEvent', {
          type, x: p.x, y: p.y, button: 'left', clickCount: 1,
          buttons: type === 'mousePressed' ? 1 : 0,
        });
      }
      await sleep(1200);
    }

    // ---- 登录
    await page.send('Page.navigate', { url: `${FRONTEND}/login` });
    await sleep(3000);
    await typeInto('.auth-field:nth-of-type(1) input', USERNAME);
    await typeInto('.auth-form .auth-password input', PASSWORD);
    await click('.auth-submit');
    await sleep(3000);
    const logged = await page.evalJson(`(function () {
      return { path: location.pathname, hasShell: !!document.querySelector('.brand-bar') };
    })()`, await ctx(':5173'));
    check(`在开发服务器上用 ${USERNAME} 登录成功`, logged.path === '/' && logged.hasShell, JSON.stringify(logged));
    if (logged.path !== '/') throw new Error(`登录未成功：${logged.path}`);

    // ---- 打开服务需求报表页
    await page.send('Page.navigate', { url: `${FRONTEND}/service-demand` });
    await sleep(3000);

    // 切到「BI 看板」标签
    const switched = await page.evalJson(`(function () {
      var tabs = Array.prototype.slice.call(document.querySelectorAll('button, .side-tabs button, .side-tabs a'));
      var hit = tabs.find(function (t) { return /BI\s*看板/.test(t.textContent || ''); });
      if (hit) { hit.click(); return true; }
      return false;
    })()`, await ctx(':5173'));
    if (switched) await sleep(2500);

    // ---- 等 iframe 里的看板加载完
    // 嵌套是三层：平台页 → iframe(/portal/, portal 的 Vue 应用) → .bi-frame(BI 模板)
    let view = null;
    for (let i = 0; i < 30; i++) {
      await sleep(900);
      view = await page.evalJson(`(function () {
        var f = document.querySelector('iframe.report-frame');
        if (!f || !f.contentDocument) return { ready: false };
        var outer = f.contentDocument;
        // BI 模板在 portal 应用内部，是第二层 iframe
        var bf = outer.querySelector('iframe.bi-frame');
        if (!bf || !bf.contentDocument) return { ready: false, stage: 'outer', outerText: (outer.body && outer.body.innerText || '').slice(0, 80) };
        var d = bf.contentDocument;
        var w = bf.contentWindow;
        var toast = d.getElementById('toast');
        var toastText = toast ? (toast.textContent || '') : '';
        // 统计卡片上的数字（kpi-value 里带 <span class="kpi-unit">%</span>，取 textContent 即可）
        var nums = Array.prototype.slice.call(d.querySelectorAll('.kpi-value'))
          .map(function (e) { return (e.textContent || '').trim(); });
        return {
          ready: true,
          src: f.getAttribute('src'),
          biSrc: bf.getAttribute('src'),
          hasTemplate: !!d.getElementById('kpiTotal'),
          base: w ? (typeof w.DEPLOY_BASE !== 'undefined' ? w.DEPLOY_BASE : null) : null,
          rowsLen: w && typeof w.allData !== 'undefined' && w.allData ? w.allData.length : null,
          hasImported: w && typeof w.hasImported !== 'undefined' ? w.hasImported : null,
          toastText: toastText,
          toastVisible: toast ? (getComputedStyle(toast).display !== 'none' && toast.style.opacity !== '0') : false,
          nums: nums,
          total: d.getElementById('kpiTotal') ? (d.getElementById('kpiTotal').textContent || '').trim() : null,
          updateTime: d.getElementById('updateTime') ? (d.getElementById('updateTime').textContent || '').trim() : null,
          canvasCount: d.querySelectorAll('canvas').length,
        };
      })()`, await ctx(':5173'));
      if (view.ready && view.hasTemplate) break;
    }

    check('iframe 里的 BI 看板模板已加载', !!(view && view.ready && view.hasTemplate),
      view ? `biSrc=${view.biSrc}` : 'iframe 未就绪');

    if (!view || !view.ready) throw new Error('看板 iframe 没加载出来，后面的检查没意义');

    // 关键断言 1：iframe 里的部署前缀推导正确
    check('iframe 内 DEPLOY_BASE 推导出 /portal 前缀', view.base === '/portal', `DEPLOY_BASE=${JSON.stringify(view.base)}`);

    // 关键断言 2：数据行数非 0
    check('看板已拿到数据行（rows > 0）', typeof view.rowsLen === 'number' && view.rowsLen > 0,
      `allData=${JSON.stringify(view.rowsLen)} hasImported=${JSON.stringify(view.hasImported)}`);

    // 关键断言 3：总需求数卡片不是 0
    check('「总需求数」卡片显示真实数值（非 0）', view.total !== null && view.total !== '' && view.total !== '0',
      `kpiTotal=${JSON.stringify(view.total)}`);

    // 关键断言 4：统计卡片不是全 0
    const nums = (view.nums || []).filter((n) => n !== '');
    const nonZero = nums.filter((n) => !/^0(\.0+)?%?$/.test(n));
    check('统计卡片出现非 0 数值', nonZero.length > 0, `卡片数值=${JSON.stringify(nums)}`);

    // 关键断言 5：没有"暂无数据"的 toast
    const noDataToast = /暂无数据|无数据/.test(view.toastText || '');
    check('未出现「后端暂无数据」提示', !noDataToast, `toast=${JSON.stringify((view.toastText || '').slice(0, 60))}`);

    // 关键断言 6：图表已绘制
    check('BI 图表 canvas 已绘制', (view.canvasCount || 0) > 0, `canvas=${view.canvasCount}`);

    // 关键断言 7：带前缀的接口真的返回 JSON 且有行
    const direct = await page.evalJson(`(function () {
      return fetch('/portal/api/bi/dataset', { credentials: 'same-origin' })
        .then(function (r) {
          return r.json().then(function (j) {
            var d = j && j.data ? j.data : j;
            return { ok: j && j.ok, rows: (d && d.rows || []).length, name: d && d.chineseName };
          });
        })
        .catch(function (e) { return { error: String(e) }; });
    })()`, await ctx(':5173'));
    check('GET /portal/api/bi/dataset 返回 JSON 且 rows 非空',
      !!(direct && direct.ok && direct.rows > 0), JSON.stringify(direct));

    // 关键断言 8：不带前缀的旧路径必须打不到数据（证明修复是必需的）
    const wrong = await page.evalJson(`(function () {
      return fetch('/api/bi/dataset', { credentials: 'same-origin' })
        .then(function (r) { return { status: r.status, type: r.headers.get('content-type') || '' }; })
        .catch(function (e) { return { error: String(e) }; });
    })()`, await ctx(':5173'));
    check('平台自身没有 /api/bi/dataset（印证原 bug 的根因）',
      !!(wrong && wrong.status && wrong.status !== 200), JSON.stringify(wrong));

    console.log('\n' + '='.repeat(56));
    const passed = results.filter((r) => r.ok).length;
    console.log(`${passed}/${results.length} PASS`);
    if (passed < results.length) {
      console.log('失败项：');
      results.filter((r) => !r.ok).forEach((r) => console.log('  - ' + r.name + '  ' + r.extra));
    }
    return passed === results.length ? 0 : 1;
  } finally {
    if (chrome) { try { chrome.kill(); } catch {} }
    if (profile) { try { rmSync(profile, { recursive: true, force: true }); } catch {} }
  }
}

main().then((code) => process.exit(code)).catch((err) => { console.error('ERROR', err.message); process.exit(1); });
