/**
 * 验证「部署到服务器后，同事从别的机器访问」这一场景真的能用。
 *
 * 做法：后端绑 0.0.0.0，用本机的**局域网 IP**（而不是 127.0.0.1）打开平台，
 * 登录后进「数据分析」，检查 iframe 是不是走 /engine/ 且工作台真的渲染出来了。
 *
 * 这个脚本专门盯一个曾经会致命的坑：
 * 内嵌地址一旦是 http://127.0.0.1:8765/，在别人机器上就变成"他自己电脑的 8765"，
 * iframe 必然白屏 —— 本地测试永远发现不了。
 *
 * 用法：node platform/tools/verify_public_deploy.mjs
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

/** 找一个非回环的 IPv4，用来模拟"别人的机器"。 */
function lanIp() {
  for (const list of Object.values(os.networkInterfaces())) {
    for (const item of list || []) {
      if (item.family === 'IPv4' && !item.internal) return item.address;
    }
  }
  return '';
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

  const host = lanIp();
  if (!host) return console.log('SKIP 找不到局域网 IP'), 0;
  console.log('模拟访问地址：', `http://${host}:<port>/`);

  const env = { ...process.env, NO_PROXY: '*', no_proxy: '*' };
  for (const key of Object.keys(env)) if (/proxy/i.test(key)) delete env[key];
  const authDir = mkdtempSync(path.join(os.tmpdir(), 'slh_deploy_'));
  env.SLH_AUTH_DB = path.join(authDir, 'auth.db');
  env.EXPOSE_API_DOCS = 'false';

  let backend = null; let chrome = null; let profile = null;
  try {
    const port = await freePort();
    backend = spawn(PYTHON, [
      '-m', 'uvicorn', 'app.main:app',
      '--host', '0.0.0.0', '--port', String(port), '--app-dir', 'platform/backend',
    ], { cwd: ROOT, env, stdio: ['ignore', 'pipe', 'pipe'] });
    backend.stderr.on('data', (c) => {
      const t = c.toString();
      if (/Traceback|ERROR/.test(t)) process.stderr.write('[api!] ' + t);
    });
    if (!(await waitHttp(`http://127.0.0.1:${port}/api/health`, 90000))) {
      throw new Error('backend did not come up');
    }
    const BASE = `http://${host}:${port}`;
    // 用局域网地址探一次，确认真的对外可访问（不只是 127.0.0.1）
    const viaLan = await waitHttp(`${BASE}/api/health`, 15000);
    check('后端绑 0.0.0.0，可用局域网 IP 访问', Boolean(viaLan));

    // 关掉接口文档：公网上不该暴露整套接口说明。
    // 注意不能只看状态码 —— /docs 被关掉后会落到 SPA fallback，照样返回 200 的 HTML，
    // 所以要看 /openapi.json 的内容类型是不是 JSON。
    const openapi = await fetch(`${BASE}/openapi.json`, { signal: AbortSignal.timeout(8000) });
    const openapiType = openapi.headers.get('content-type') || '';
    check('/openapi.json 已关闭（不再返回 JSON）', !openapiType.includes('application/json'),
      `type=${openapiType.split(';')[0]}`);

    // 建管理员
    const seeded = await fetch(`${BASE}/api/auth/setup`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: '95', password: '123456000', display_name: '孟鹏杰' }),
    }).then((r) => r.json()).catch(() => null);
    check('首次设置管理员可用', Boolean(seeded?.user), seeded?.detail || '');

    // 未登录时引擎入口必须挡住
    const engineAnon = await fetch(`${BASE}/engine/`, {
      redirect: 'manual', signal: AbortSignal.timeout(8000),
    });
    check('/engine/ 未登录被拦（401）', engineAnon.status === 401, `status=${engineAnon.status}`);

    const debugPort = await freePort();
    profile = mkdtempSync(path.join(os.tmpdir(), 'slh_deploy_chrome_'));
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
    const afterLogin = await page.evalJson(`(function () {
      return { path: location.pathname, hasShell: !!document.querySelector('.brand-bar') };
    })()`, await ctx());
    check('用局域网地址登录成功', afterLogin.path === '/' && afterLogin.hasShell, JSON.stringify(afterLogin));

    // 进数据分析页
    await page.evalJson(`(function(){ location.hash=''; return true; })()`, await ctx());
    await page.send('Page.navigate', { url: `${BASE}/data-analysis` });
    await sleep(2000);
    await page.send('Runtime.enable');

    // 等 iframe 加载出来
    let frame = null;
    for (let i = 0; i < 30; i++) {
      await sleep(1000);
      frame = await page.evalJson(`(function () {
        var f = document.querySelector('iframe.embed-frame');
        return f ? { src: f.getAttribute('src'), docReady: !!f.contentDocument } : null;
      })()`, await ctx()).catch(() => null);
      if (frame && frame.src) break;
    }
    check('数据分析 iframe 存在', Boolean(frame), JSON.stringify(frame));
    check('iframe 走 /engine/ 而不是 127.0.0.1', frame && frame.src.startsWith('/engine'), frame?.src || '');
    check('iframe 地址里没有 127.0.0.1', frame && !String(frame.src).includes('127.0.0.1'), frame?.src || '');

    // 等引擎页面在内嵌框架里真正渲染（同源，可直接读 contentDocument）
    let workbench = null;
    for (let i = 0; i < 30; i++) {
      await sleep(1000);
      workbench = await page.evalJson(`(function () {
        var f = document.querySelector('iframe.embed-frame');
        if (!f || !f.contentDocument) return null;
        var d = f.contentDocument;
        var links = [...d.querySelectorAll('.nav a[data-key]')].map(function (a) { return a.dataset.key; });
        return {
          title: d.title,
          navKeys: links,
          hasMaccura: !!d.querySelector('.brand-maccura, .brand'),
          bodyLen: d.body ? d.body.innerText.length : 0,
        };
      })()`, await ctx()).catch(() => null);
      if (workbench && workbench.navKeys && workbench.navKeys.length) break;
    }
    check('工作台在内嵌框架里渲染出来了', Boolean(workbench && workbench.bodyLen > 50),
      `title=${workbench?.title} 菜单=${workbench?.navKeys?.join('/')}`);
    check('工作台菜单项齐全（8 个，AI 已隐藏）',
      Boolean(workbench?.navKeys?.includes('home') && !workbench?.navKeys?.includes('ai')),
      (workbench?.navKeys || []).join('/'));

    // 引擎自己的接口经 /engine/api 走得通
    const engineApi = await page.evalJson(`(async function () {
      var r = await fetch('/engine/api/state');
      var t = await r.text();
      var parsed = null;
      try { parsed = JSON.parse(t); } catch (e) { /* 不是 JSON 就留 null */ }
      return { status: r.status, ok: !!(parsed && parsed.ok === true) };
    })()`, await ctx());
    check('引擎接口经 /engine/api 走通', engineApi.status === 200 && engineApi.ok, `status=${engineApi.status}`);

    try {
      const shot = await page.send('Page.captureScreenshot', { format: 'png' });
      const shotPath = path.join(os.tmpdir(), 'slh_public_deploy.png');
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
