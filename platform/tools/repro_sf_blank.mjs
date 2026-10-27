/**
 * 复现「点击后是空白页」：用**真实鼠标点击**（Input.dispatchMouseEvent，有用户手势）
 * 点「查看全部」，同时抓全部网络请求，看新标签页到底卡在哪一步。
 *
 * 用法：node platform/tools/repro_sf_blank.mjs
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
const PYTHON = process.env.SLH_PYTHON || 'C:\\Users\\98319\\.workbuddy\\binaries\\python\\envs\\smartlabhub\\Scripts\\python.exe';
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitHttp(url, timeoutMs) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    try {
      const response = await fetch(url, { signal: AbortSignal.timeout(2500) });
      if (response.ok) return await response.text();
    } catch {}
    await sleep(400);
  }
  return null;
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

class Cdp {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    this.contexts = new Map();
    this.requests = [];
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
      if (message.method === 'Network.requestWillBeSent') {
        this.requests.push({
          t: Date.now(),
          method: message.params.request.method,
          url: message.params.request.url,
          type: message.params.type,
        });
      }
      if (message.method === 'Network.responseReceived') {
        this.requests.push({
          t: Date.now(),
          resp: message.params.response.status,
          url: message.params.response.url,
        });
      }
    });
  }
  send(method, params = {}, timeoutMs = 45000) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error(`timeout: ${method}`)); }, timeoutMs);
      this.pending.set(id, { resolve: (m) => { clearTimeout(timer); resolve(m); } });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  async evaluate(expression, contextId) {
    const params = { expression, returnByValue: true, awaitPromise: true };
    if (contextId) params.contextId = contextId;
    const response = await this.send('Runtime.evaluate', params);
    const value = response?.result?.result?.value;
    if (value === undefined) throw new Error(`evaluate failed: ${JSON.stringify(response).slice(0, 300)}`);
    return value;
  }
  async evalJson(expression, contextId) {
    return JSON.parse(await this.evaluate(`(async function () { return JSON.stringify(await (${expression})); })()`, contextId));
  }
}

function openWs(url) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(url);
    ws.addEventListener('open', () => resolve(ws), { once: true });
    ws.addEventListener('error', () => reject(new Error('websocket error')), { once: true });
  });
}

async function getJson(url) {
  try {
    const response = await fetch(url, { signal: AbortSignal.timeout(3000) });
    return await response.json();
  } catch {
    return null;
  }
}

async function main() {
  if (!existsSync(CHROME)) return console.log('SKIP chrome missing'), 0;
  if (!existsSync(PYTHON)) return console.log('SKIP python missing'), 0;

  const env = { ...process.env, NO_PROXY: '*', no_proxy: '*' };
  for (const key of Object.keys(env)) if (/proxy/i.test(key)) delete env[key];

  let backend = null; let frontend = null; let chrome = null; let profile = null;
  try {
    const backendPort = await freePort();
    const frontendPort = await freePort();
    backend = spawn(PYTHON, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(backendPort), '--app-dir', 'platform/backend'], {
      cwd: ROOT, env, stdio: ['ignore', 'pipe', 'pipe'],
    });
    backend.stderr.on('data', (c) => {
      const t = c.toString();
      if (/salesforce|launch|ERROR|Traceback/.test(t)) process.stderr.write('[api!] ' + t);
    });
    if (!(await waitHttp(`http://127.0.0.1:${backendPort}/api/health`, 90000))) throw new Error('backend did not come up');
    frontend = spawn(process.execPath, [path.join(ROOT, 'home', 'node_modules', 'vite', 'bin', 'vite.js'), '--host', '127.0.0.1', '--port', String(frontendPort), '--strictPort'], {
      cwd: path.join(ROOT, 'home'),
      env: { ...env, SLH_API_TARGET: `http://127.0.0.1:${backendPort}` },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    if (!(await waitHttp(`http://127.0.0.1:${frontendPort}/`, 90000))) throw new Error('frontend did not come up');

    const debugPort = await freePort();
    profile = mkdtempSync(path.join(os.tmpdir(), 'slh_sf_blank_'));
    chrome = spawn(CHROME, [
      `--remote-debugging-port=${debugPort}`, `--user-data-dir=${profile}`,
      '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
      '--no-proxy-server', '--proxy-bypass-list=*', '--window-size=1500,1100', 'about:blank',
    ], { env, stdio: 'ignore' });

    let version = null;
    const versionEnd = Date.now() + 25000;
    while (Date.now() < versionEnd && !version) {
      version = await getJson(`http://127.0.0.1:${debugPort}/json/version`);
      if (!version) await sleep(300);
    }
    if (!version) throw new Error('devtools unreachable');
    console.log('CHROME', version.Browser);

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
    await page.send('Network.enable');
    await page.send('Page.navigate', { url: `http://127.0.0.1:${frontendPort}/` });

    let ctxId = null;
    const rEnd = Date.now() + 60000;
    while (Date.now() < rEnd && !ctxId) {
      await sleep(800);
      ctxId = [...page.contexts.values()].find((c) => c.origin.includes(`127.0.0.1:${frontendPort}`))?.id || null;
    }
    if (!ctxId) throw new Error('Vue 页面未建立上下文');

    // 等 Salesforce 板块数据出来
    let ready = false;
    const sEnd = Date.now() + 120000;
    while (Date.now() < sEnd && !ready) {
      await sleep(1000);
      try {
        ready = await page.evalJson(`(function () {
          var b = [...document.querySelectorAll('.sf-quick-action')].find(function (x) { return x.textContent.indexOf('查看全部') >= 0; });
          return !!(b && b.getBoundingClientRect().width > 0);
        })()`, ctxId);
      } catch {}
    }
    if (!ready) throw new Error('没找到可点的「查看全部」按钮');

    // 记录点击前的标签页
    const before = await fetch(`http://127.0.0.1:${debugPort}/json/list`).then((r) => r.json()).catch(() => []);
    console.log('点击前标签页:', JSON.stringify(before.filter((t) => t.type === 'page').map((t) => ({ id: t.id, url: (t.url || '').slice(0, 60) }))));

    // ★ 在点击前 hook window.open，统计它被调用了几次、参数是什么
    await page.evalJson(`(function () {
      window.__openCalls = [];
      var nativeOpen = window.open;
      window.open = function (url, target, features) {
        var w = nativeOpen.apply(window, arguments);
        window.__openCalls.push({
          url: String(url), target: String(target), features: String(features),
          returned: !!w,
          href: w ? String(w.location.href) : null,
        });
        return w;
      };
      return true;
    })()`, ctxId);

    // ★ 真实鼠标点击（带用户手势，window.open 才不会被拦）
    const pos = await page.evalJson(`(function () {
      var b = [...document.querySelectorAll('.sf-quick-action')].find(function (x) { return x.textContent.indexOf('查看全部') >= 0; });
      b.scrollIntoView({block:'center'});
      return true;
    })()`, ctxId);
    await sleep(800);
    const coords = await page.evalJson(`(function () {
      var b = [...document.querySelectorAll('.sf-quick-action')].find(function (x) { return x.textContent.indexOf('查看全部') >= 0; });
      var r = b.getBoundingClientRect();
      return { x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2), label: b.textContent.trim() };
    })()`, ctxId);
    console.log('CLICK_AT', JSON.stringify(coords));
    for (const type of ['mousePressed', 'mouseReleased']) {
      await page.send('Input.dispatchMouseEvent', {
        type, x: coords.x, y: coords.y, button: 'left', clickCount: 1,
        buttons: type === 'mousePressed' ? 1 : 0,
      });
    }

    // 观察 12 秒内发生什么
    for (let i = 0; i < 6; i++) {
      await sleep(2000);
      const list = await fetch(`http://127.0.0.1:${debugPort}/json/list`).then((r) => r.json()).catch(() => []);
      const pages = list.filter((t) => t.type === 'page');
      console.log(`T+${(i + 1) * 2}s 标签页:`, JSON.stringify(pages.map((t) => (t.url || '').slice(0, 90))));
    }

    // 判定：只应多出 1 个标签页，且落在 Lightning 任务列表；平台页不能被跳走；不能留 about:blank
    const afterList = await fetch(`http://127.0.0.1:${debugPort}/json/list`).then((r) => r.json()).catch(() => []);
    const afterPages = afterList.filter((t) => t.type === 'page');
    const beforeIds = new Set(before.filter((t) => t.type === 'page').map((t) => t.id));
    const newTabs = afterPages.filter((t) => !beforeIds.has(t.id));
    const blankTabs = afterPages.filter((t) => (t.url || '') === 'about:blank');
    const platformStill = afterPages.some((t) => (t.url || '').includes(`127.0.0.1:${frontendPort}`));
    console.log('\n===== 判定 =====');
    console.log(' 新增标签页数:', newTabs.length, newTabs.map((t) => (t.url || '').slice(0, 80)));
    console.log(' 残留 about:blank:', blankTabs.length);
    console.log(' 平台页还在原处:', platformStill);
    if (newTabs.length !== 1) throw new Error(`新增标签页数不对: ${newTabs.length}（应恰好 1 个）`);
    if (!/lightning\/o\/Task\/list/.test(newTabs[0].url || '')) throw new Error('新标签页没落在任务列表: ' + newTabs[0].url);
    if (!platformStill) throw new Error('平台页被跳走了 —— 说明 openSalesforce 误判了弹窗被拦');
    if (blankTabs.length > before.filter((t) => (t.url || '') === 'about:blank').length) {
      throw new Error('留下了 about:blank 空白标签页');
    }
    console.log('\n===== 平台页发出的请求 =====');
    for (const r of page.requests) {
      if (/launch|salesforce/i.test(r.url)) {
        console.log(` ${r.method || 'RESP'} ${r.resp || ''} ${r.url.slice(0, 130)}`);
      }
    }

    const opens = await page.evalJson(`(function () { return window.__openCalls || []; })()`, ctxId).catch(() => []);
    console.log('\n===== window.open 调用记录 =====');
    console.log(JSON.stringify(opens, null, 2));

    const after = await fetch(`http://127.0.0.1:${debugPort}/json/list`).then((r) => r.json()).catch(() => []);
    console.log('\n===== 点击后标签页（含 id） =====');
    for (const t of after.filter((x) => x.type === 'page')) {
      const wasBefore = before.some((b) => b.id === t.id);
      console.log(` ${wasBefore ? '原有' : '新建'} ${t.id.slice(0, 10)} ${(t.url || '').slice(0, 80)}`);
    }
    console.log('\n===== 全部请求尾部 =====');
    for (const r of page.requests.slice(-12)) {
      console.log(` ${r.method || 'RESP'} ${r.resp || ''} ${r.url.slice(0, 110)}`);
    }
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
