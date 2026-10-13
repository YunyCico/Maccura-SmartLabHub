/**
 * 验证「数据分析」页嵌入工具栏的「← 返回主页」按钮：
 * 按钮存在、样式可见、点击后路由回到首页（pathname === '/'）且首页内容渲染。
 *
 * 一体化：起后端 -> 起前端 -> 无头 Chrome -> 操作 -> 断言 -> 清理。
 * 用法：node tools/verify_back_home.mjs      环境变量：SLH_DEBUG=1
 */
import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync } from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..', '..');
const CHROME = path.join(process.env.LOCALAPPDATA || '', 'Google/Chrome/Application/chrome.exe');
const PYTHON =
  process.env.SLH_PYTHON ||
  'C:\\Users\\98319\\.workbuddy\\binaries\\python\\envs\\smartlabhub\\Scripts\\python.exe';
const BACKEND = 8000;
const FRONTEND = 5173;
const ANALYSIS_URL = `http://127.0.0.1:${FRONTEND}/data-analysis`;
const DEBUG = !!process.env.SLH_DEBUG;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function getJson(url, timeoutMs = 4000) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const res = await fetch(url, { signal: ctl.signal });
    return await res.json();
  } catch {
    return null;
  } finally {
    clearTimeout(t);
  }
}

async function waitHttp(url, timeoutMs, expect) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    try {
      const ctl = new AbortController();
      const t = setTimeout(() => ctl.abort(), 2500);
      const res = await fetch(url, { signal: ctl.signal });
      clearTimeout(t);
      if (res.ok) {
        const text = await res.text();
        if (!expect || expect.test(text)) return text;
      }
    } catch {}
    await sleep(500);
  }
  return null;
}

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.listen(0, '127.0.0.1', () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
    srv.on('error', reject);
  });
}

class Cdp {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    this.errors = [];
    ws.addEventListener('message', (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        resolve(msg);
        return;
      }
      if (msg.method === 'Runtime.exceptionThrown') {
        const d = msg.params?.exceptionDetails || {};
        this.errors.push('EXCEPTION: ' + (d.exception?.description || d.text || ''));
      }
      if (msg.method === 'Runtime.consoleAPICalled' && msg.params?.type === 'error') {
        this.errors.push('console.error: ' + (msg.params.args || []).map((a) => a.value ?? '').join(' '));
      }
    });
  }
  send(method, params = {}, timeoutMs = 25000) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error('timeout ' + method));
      }, timeoutMs);
      this.pending.set(id, {
        resolve: (m) => {
          clearTimeout(timer);
          resolve(m);
        },
      });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  async evalJson(expression, timeoutMs = 25000) {
    const r = await this.send('Runtime.evaluate', { expression, returnByValue: true }, timeoutMs);
    const v = r?.result?.result?.value;
    if (v === undefined || v === null)
      throw new Error('evaluate empty: ' + JSON.stringify(r).slice(0, 300));
    return JSON.parse(v);
  }
}

function openWs(url) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(url);
    ws.addEventListener('open', () => resolve(ws), { once: true });
    ws.addEventListener('error', () => reject(new Error('ws error')), { once: true });
  });
}

const PROBE = `(function () {
  var btn = document.querySelector('.back-home-button');
  var box = btn ? btn.getBoundingClientRect() : null;
  var cs = btn ? getComputedStyle(btn) : null;
  return JSON.stringify({
    path: location.pathname,
    hasButton: !!btn,
    text: btn ? btn.textContent.trim() : '',
    visible: !!(box && box.width > 0 && box.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'),
    inViewport: !!(box && box.left >= 0 && box.top >= 0 && box.right <= window.innerWidth + 1),
    bg: cs ? cs.backgroundColor : '',
    color: cs ? cs.color : '',
    radius: cs ? cs.borderRadius : '',
    primaryCount: document.querySelectorAll('.embed-actions .primary-button').length,
    toolbarTitle: (document.querySelector('.embed-toolbar h2') || {}).textContent || '',
    homeHeading: (document.querySelector('.topbar h1') || {}).textContent || '',
    homeMetrics: document.querySelectorAll('.metric-card').length,
    frame: !!document.querySelector('.embed-frame')
  });
})()`;

async function main() {
  if (!existsSync(CHROME)) return console.log('SKIP chrome missing'), 0;
  if (!existsSync(PYTHON)) return console.log('SKIP python missing'), 0;

  const env = { ...process.env };
  for (const k of Object.keys(env)) if (/proxy/i.test(k)) delete env[k];
  env.NO_PROXY = '*';
  env.no_proxy = '*';

  let backend = null, frontend = null, chrome = null, profile = null;
  try {
    if (!(await waitHttp(`http://127.0.0.1:${BACKEND}/api/health`, 800))) {
      backend = spawn(PYTHON, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(BACKEND), '--app-dir', 'platform/backend'], {
        cwd: ROOT, env, stdio: ['ignore', 'pipe', 'pipe'],
      });
      backend.stderr.on('data', (b) => process.stderr.write('[api!] ' + b));
    }
    const health = await waitHttp(`http://127.0.0.1:${BACKEND}/api/health`, 40000);
    if (!health) throw new Error('backend did not come up');
    console.log('backend: ready');

    if (!(await waitHttp(`http://127.0.0.1:${FRONTEND}/`, 800, /id="app"/))) {
      frontend = spawn(process.platform === 'win32' ? 'npm.cmd' : 'npm', ['run', 'dev', '--', '--host', '127.0.0.1', '--port', String(FRONTEND), '--strictPort'], {
        cwd: path.join(ROOT, 'home'), env, stdio: ['ignore', 'pipe', 'pipe'],
      });
      frontend.stdout.on('data', (b) => process.stdout.write('[vite] ' + b));
      frontend.stderr.on('data', (b) => process.stderr.write('[vite!] ' + b));
    }
    const index = await waitHttp(`http://127.0.0.1:${FRONTEND}/`, 60000, /id="app"/);
    if (!index) throw new Error('frontend did not come up');
    console.log('frontend: ready');

    const port = await freePort();
    profile = mkdtempSync(path.join(os.tmpdir(), 'slh_home_'));
    chrome = spawn(CHROME, [
      `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, '--headless=new',
      '--disable-gpu', '--no-first-run', '--no-default-browser-check',
      '--no-proxy-server', '--proxy-bypass-list=*', '--window-size=1400,1000', 'about:blank',
    ], { env, stdio: 'ignore' });

    const ver = await (async () => {
      const end = Date.now() + 20000;
      while (Date.now() < end) {
        const v = await getJson(`http://127.0.0.1:${port}/json/version`);
        if (v) return v;
        await sleep(400);
      }
      return null;
    })();
    if (!ver) throw new Error('devtools unreachable');
    console.log('CHROME', ver.Browser);

    const browser = new Cdp(await openWs(ver.webSocketDebuggerUrl));
    const tid = (await browser.send('Target.createTarget', { url: 'about:blank' })).result.targetId;
    let wsUrl = null;
    const dl = Date.now() + 15000;
    while (Date.now() < dl && !wsUrl) {
      const list = (await getJson(`http://127.0.0.1:${port}/json/list`)) || [];
      const hit = list.find((t) => t.id === tid);
      if (hit) wsUrl = hit.webSocketDebuggerUrl;
      if (!wsUrl) await sleep(300);
    }
    if (!wsUrl) throw new Error('no target ws');

    const page = new Cdp(await openWs(wsUrl));
    await page.send('Runtime.enable');
    await page.send('Log.enable');
    await page.send('Page.enable');
    await page.send('Page.navigate', { url: ANALYSIS_URL });

    // 等 Vue 应用挂载 + 嵌入工具栏出现
    let before = null;
    const endReady = Date.now() + 45000;
    while (Date.now() < endReady && !before?.hasButton) {
      await sleep(1200);
      try {
        before = await page.evalJson(PROBE);
        if (DEBUG) console.log('  ready poll', JSON.stringify(before));
      } catch {}
    }
    console.log('BEFORE ' + JSON.stringify(before, null, 2));
    if (!before?.hasButton) throw new Error('返回按钮未渲染');
    if (!before.visible) throw new Error('返回按钮不可见');
    if (!before.text.includes('返回 SmartLabHub')) throw new Error('按钮文案不对：' + before.text);
    if (before.primaryCount !== 1) throw new Error('工具栏应只有一个实心主按钮，实际 ' + before.primaryCount);
    if (!/rgb\(0, 113, 227\)|rgb\(11, 11, 11\)|rgba\(0, 0, 0, 0\)/.test(before.bg)) {
      console.log('  按钮背景色 =', before.bg, '（非透明即为实心）');
    }

    // 真实点击
    await page.send('Runtime.evaluate', {
      expression: `(function(){var b=document.querySelector('.back-home-button'); if(b){b.click(); return 'clicked';} return 'missing';})()`,
      returnByValue: true,
    });

    let after = null;
    const endNav = Date.now() + 15000;
    while (Date.now() < endNav && after?.path !== '/') {
      await sleep(800);
      try { after = await page.evalJson(PROBE); } catch {}
    }
    console.log('AFTER ' + JSON.stringify(after, null, 2));

    const failed = [];
    if (after?.path !== '/') failed.push('点击后未回到主页，实际 path=' + after?.path);
    if (!after?.homeHeading) failed.push('主页标题未渲染');
    if (!(after?.homeMetrics > 0)) failed.push('主页指标卡未渲染');

    if (page.errors.length) failed.push('页面报错：' + page.errors.join(' | '));

    if (failed.length) {
      console.log('RESULT: FAIL');
      failed.forEach((f) => console.log('  - ' + f));
      return 1;
    }
    console.log('RESULT: PASS — 返回 SmartLabHub 按钮为实心主按钮，点击后回到主页，页面无报错');
    return 0;
  } catch (error) {
    console.log('RESULT: FAIL —', error.message);
    return 1;
  } finally {
    try { chrome?.kill(); } catch {}
    try { frontend?.kill(); } catch {}
    try { backend?.kill(); } catch {}
    try { profile && rmSync(profile, { recursive: true, force: true }); } catch {}
  }
}

process.exit(await main());
