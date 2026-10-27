/**
 * 验证「服务需求报表」模块已整合进平台：
 *   1. 首页「部门工作模块」里有这张卡片
 *   2. 点进去到 /service-demand，iframe 指向 /portal/
 *   3. iframe 里真的渲染出数据中台页面（不是白屏 / 404）
 *   4. 未登录时 /portal/ 被 401 挡住
 *   5. 子应用的接口经 /portal/api 走得通
 *
 * 用法：node platform/tools/verify_service_demand.mjs
 */
import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..', '..');
const CHROME = path.join(process.env.LOCALAPPDATA || '', 'Google/Chrome/Application/chrome.exe');
const PYTHON = process.env.SLH_PYTHON || 'C:\\Users\\98319\\.workbuddy\\binaries\\python\\envs\\smartlabhub\\Scripts\\python.exe';
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
    if (value === undefined) throw new Error('evaluate failed: ' + JSON.stringify(response).slice(0, 200));
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

async function getJson(url) {
  try {
    const response = await fetch(url, { signal: AbortSignal.timeout(3000) });
    return await response.json();
  } catch { return null; }
}

async function main() {
  if (!existsSync(CHROME)) return console.log('SKIP chrome missing'), 0;
  if (!existsSync(PYTHON)) return console.log('SKIP python missing'), 0;

  const env = { ...process.env, NO_PROXY: '*', no_proxy: '*' };
  for (const key of Object.keys(env)) if (/proxy/i.test(key)) delete env[key];
  const authDir = mkdtempSync(path.join(os.tmpdir(), 'slh_sd_'));
  env.SLH_AUTH_DB = path.join(authDir, 'auth.db');

  let backend = null; let chrome = null; let profile = null;
  try {
    const port = await freePort();
    backend = spawn(PYTHON, [
      '-m', 'uvicorn', 'app.main:app',
      '--host', '127.0.0.1', '--port', String(port), '--app-dir', 'platform/backend',
    ], { cwd: ROOT, env, stdio: ['ignore', 'pipe', 'pipe'] });
    backend.stderr.on('data', (c) => {
      const t = c.toString();
      if (/Traceback|\[WARN\]/.test(t)) process.stderr.write('[api] ' + t);
    });
    if (!(await waitHttp(`http://127.0.0.1:${port}/api/health`, 90000))) {
      throw new Error('backend did not come up');
    }
    const BASE = `http://127.0.0.1:${port}`;

    // 未登录：子应用要被挡住
    const anon = await fetch(`${BASE}/portal/`, { redirect: 'manual', signal: AbortSignal.timeout(8000) });
    check('未登录访问 /portal/ 被拦（401）', anon.status === 401, `status=${anon.status}`);

    // 建管理员
    const seeded = await fetch(`${BASE}/api/auth/setup`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: '95', password: '123456000', display_name: '孟鹏杰' }),
    }).then((r) => r.json()).catch(() => null);
    check('首次设置管理员可用', Boolean(seeded?.user), seeded?.detail || '');

    const debugPort = await freePort();
    profile = mkdtempSync(path.join(os.tmpdir(), 'slh_sd_chrome_'));
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

    async function ctx() {
      for (let i = 0; i < 40; i++) {
        const c = [...page.contexts.values()].find((x) => x.origin.includes(`:${port}`))?.id;
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
      })()`, await ctx());
    }
    async function click(selector) {
      const p = await page.evalJson(`(function () {
        var el = document.querySelector('${selector}');
        if (!el) return null;
        el.scrollIntoView({block:'center'});
        var r = el.getBoundingClientRect();
        return { x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2) };
      })()`, await ctx());
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

    // 登录
    await page.send('Page.navigate', { url: `${BASE}/login` });
    await sleep(3000);
    await typeInto('.auth-field:nth-of-type(1) input', '95');
    await typeInto('.auth-form .auth-password input', '123456000');
    await click('.auth-submit');
    await sleep(2500);

    // 首页模块卡片
    const cards = await page.evalJson(`(function () {
      var list = [...document.querySelectorAll('.module-card')].map(function (c) {
        return {
          label: (c.querySelector('strong') || {}).textContent || '',
          tone: c.className,
        };
      });
      return { count: list.length, labels: list.map(function (x) { return x.label.trim(); }), raw: list };
    })()`, await ctx());
    check('首页模块数变成 5 个', cards.count === 5, cards.labels.join(' / '));
    check('有「服务需求报表」卡片', cards.labels.includes('服务需求报表'), cards.labels.join(' / '));

    // 找到卡片并点进去
    const clicked = await page.evalJson(`(function () {
      var target = [...document.querySelectorAll('.module-card')].find(function (c) {
        return (c.querySelector('strong') || {}).textContent.trim() === '服务需求报表';
      });
      if (!target) return false;
      target.click();
      return true;
    })()`, await ctx());
    check('能点进服务需求报表', clicked === true);
    await sleep(2500);

    const route = await page.evalJson(`(function () { return { path: location.pathname }; })()`, await ctx());
    check('路由跳到 /service-demand', route.path === '/service-demand', route.path);

    // iframe
    let frame = null;
    for (let i = 0; i < 30; i++) {
      await sleep(800);
      frame = await page.evalJson(`(function () {
        var f = document.querySelector('iframe.report-frame');
        return f ? { src: f.getAttribute('src') } : null;
      })()`, await ctx()).catch(() => null);
      if (frame && frame.src) break;
    }
    check('iframe 指向 /portal/', Boolean(frame && frame.src === '/portal/'), frame?.src || '');

    // iframe 内渲染（同源可直接读）
    let inner = null;
    for (let i = 0; i < 30; i++) {
      await sleep(800);
      inner = await page.evalJson(`(function () {
        var f = document.querySelector('iframe.report-frame');
        if (!f || !f.contentDocument) return null;
        var d = f.contentDocument;
        return {
          title: d.title,
          bodyLen: d.body ? d.body.innerText.replace(/\\s+/g,' ').trim().length : 0,
          head: d.body ? d.body.innerText.replace(/\\s+/g,' ').trim().slice(0, 120) : '',
          hasAssets: !!d.querySelector('.app, #app, main'),
        };
      })()`, await ctx()).catch(() => null);
      if (inner && inner.bodyLen > 20) break;
    }
    check('报表页面在 iframe 里渲染出来了', Boolean(inner && inner.bodyLen > 20),
      `title=${inner?.title} 正文字数=${inner?.bodyLen}`);
    check('是数据中台的界面', Boolean(inner && /数据中台|服务需求|拉取|看板/.test(inner.head)),
      inner?.head || '');

    // 子应用接口
    const apiOk = await page.evalJson(`(async function () {
      var r = await fetch('/portal/api/config');
      var t = await r.text();
      var j = null;
      try { j = JSON.parse(t); } catch (e) {}
      return { status: r.status, ok: !!(j && j.ok) };
    })()`, await ctx());
    check('子应用接口经 /portal/api 走通', apiOk.status === 200 && apiOk.ok, `status=${apiOk.status}`);

    try {
      const shot = await page.send('Page.captureScreenshot', { format: 'png' });
      const shotPath = path.join(os.tmpdir(), 'slh_service_demand.png');
      writeFileSync(shotPath, Buffer.from(shot.result.data, 'base64'));
      console.log('SCREENSHOT', shotPath);
    } catch {}

    const failed = results.filter((r) => !r.ok);
    console.log(`\n结果：${results.length - failed.length}/${results.length} 通过`);
    return failed.length === 0 ? 0 : 1;
  } catch (error) {
    console.log('RESULT: FAIL —', error.message);
    return 1;
  } finally {
    try { chrome?.kill(); } catch {}
    try { backend?.kill(); } catch {}
    try { profile && rmSync(profile, { recursive: true, force: true }); } catch {}
    try { authDir && rmSync(authDir, { recursive: true, force: true }); } catch {}
  }
}

process.exit(await main());
