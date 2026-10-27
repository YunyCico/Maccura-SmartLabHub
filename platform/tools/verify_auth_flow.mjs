/**
 * 真实浏览器验证账号模式：
 *   1. 首次打开 -> 自动进「首次设置管理员」
 *   2. 建管理员 -> 自动登录进首页
 *   3. 退出 -> 回登录页
 *   4. 密码错 -> 有报错且不进首页
 *   5. 密码对 -> 进首页，顶部显示用户名
 *   6. 深链接未登录 -> 顶到登录页；登录后跳回原地址
 *   7. 普通成员登录 -> 看不到账号管理入口
 *
 * 用法：node platform/tools/verify_auth_flow.mjs
 *
 * ⚠️ 用临时账号库（SLH_AUTH_DB），不碰真实账号。
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

const results = [];
function check(name, ok, extra = '') {
  results.push({ name, ok, extra });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${extra ? '  ' + extra : ''}`);
}

async function main() {
  if (!existsSync(CHROME)) return console.log('SKIP chrome missing'), 0;
  if (!existsSync(PYTHON)) return console.log('SKIP python missing'), 0;

  const env = { ...process.env, NO_PROXY: '*', no_proxy: '*' };
  for (const key of Object.keys(env)) if (/proxy/i.test(key)) delete env[key];
  // 账号库存到临时目录，不碰真实账号
  const authDir = mkdtempSync(path.join(os.tmpdir(), 'slh_auth_'));
  env.SLH_AUTH_DB = path.join(authDir, 'auth.db');

  let backend = null; let frontend = null; let chrome = null; let profile = null;
  try {
    const backendPort = await freePort();
    const frontendPort = await freePort();
    backend = spawn(PYTHON, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(backendPort), '--app-dir', 'platform/backend'], {
      cwd: ROOT, env, stdio: ['ignore', 'pipe', 'pipe'],
    });
    backend.stderr.on('data', (c) => {
      const t = c.toString();
      if (/Traceback|ERROR/.test(t)) process.stderr.write('[api!] ' + t);
    });
    if (!(await waitHttp(`http://127.0.0.1:${backendPort}/api/health`, 90000))) throw new Error('backend did not come up');

    frontend = spawn(process.execPath, [path.join(ROOT, 'home', 'node_modules', 'vite', 'bin', 'vite.js'), '--host', '127.0.0.1', '--port', String(frontendPort), '--strictPort'], {
      cwd: path.join(ROOT, 'home'),
      env: { ...env, SLH_API_TARGET: `http://127.0.0.1:${backendPort}` },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    if (!(await waitHttp(`http://127.0.0.1:${frontendPort}/`, 90000))) throw new Error('frontend did not come up');

    const debugPort = await freePort();
    profile = mkdtempSync(path.join(os.tmpdir(), 'slh_auth_chrome_'));
    chrome = spawn(CHROME, [
      `--remote-debugging-port=${debugPort}`, `--user-data-dir=${profile}`,
      '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
      '--no-proxy-server', '--proxy-bypass-list=*', '--window-size=1400,1000', 'about:blank',
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

    const BASE = `http://127.0.0.1:${frontendPort}`;
    async function navigate(url) {
      // 整页导航后旧的执行上下文会失效，先清掉再强制重推一遍
      page.contexts.clear();
      await page.send('Page.navigate', { url });
      await sleep(1500);
      try {
        await page.send('Runtime.disable');
        await page.send('Runtime.enable');
      } catch {}
      await sleep(600);
    }
    async function ctx() {
      for (let i = 0; i < 40; i++) {
        const c = [...page.contexts.values()].find((x) => x.origin.includes(`127.0.0.1:${frontendPort}`))?.id;
        if (c) return c;
        await sleep(300);
      }
      return null;
    }
    async function probe() {
      const c = await ctx();
      if (!c) return {};
      return page.evalJson(`(function () {
        var body = document.body ? document.body.innerText : '';
        return {
          path: location.pathname,
          title: document.title,
          body: body.replace(/\\s+/g, ' ').slice(0, 260),
          hasAccountLink: /账号管理/.test(body),
          hasUserBlock: !!document.querySelector('.user-block'),
          hasLoginForm: !!document.querySelector('.auth-submit'),
        };
      })()`, c).catch(() => ({}));
    }
    async function typeInto(selector, value) {
      const c = await ctx();
      await page.evalJson(`(function () {
        var el = document.querySelector('${selector}');
        var setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
        setter.call(el, ${JSON.stringify(value)});
        el.dispatchEvent(new Event('input', { bubbles: true }));
        return true;
      })()`, c);
    }
    async function click(selector) {
      const c = await ctx();
      const p = await page.evalJson(`(function () {
        var el = document.querySelector('${selector}');
        if (!el) return null;
        el.scrollIntoView({block:'center'});
        var r = el.getBoundingClientRect();
        return { x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2) };
      })()`, c);
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

    // ---- 1) 首次打开 -> 自动进 /setup
    await navigate(BASE + '/');
    let s = await probe();
    check('首次打开自动进「首次设置」', s.path === '/setup', `path=${s.path}`);

    // ---- 2) 建管理员
    await typeInto('.auth-field:nth-of-type(1) input', 'yun');
    await typeInto('.auth-field:nth-of-type(2) input', '孟鹏杰');
    await typeInto('.auth-form .auth-password input', 'YunPass123!');
    const pwdInputs = await page.evalJson(`document.querySelectorAll('.auth-form input[type=password]').length`, await ctx());
    // 第二个密码框是"确认密码"
    const c2 = await ctx();
    await page.evalJson(`(function () {
      var list = document.querySelectorAll('.auth-form input');
      var target = null;
      list.forEach(function (el) { if (el.closest('.auth-field') && el.type === 'password' && el.closest('.auth-field').querySelector('span').textContent.indexOf('确认') >= 0) target = el; });
      if (target) {
        var setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
        setter.call(target, 'YunPass123!');
        target.dispatchEvent(new Event('input', { bubbles: true }));
      }
      return true;
    })()`, c2);
    await sleep(400);
    await click('.auth-submit');
    await sleep(2000);
    s = await probe();
    check('建完管理员自动登录进首页', s.path === '/' && s.hasUserBlock, `path=${s.path}`);
    check('顶部显示用户信息', /孟鹏杰/.test(s.body), s.body.slice(0, 60));

    // ---- 3) 管理员能看到账号管理入口
    check('管理员首页有「账号管理」入口', s.hasAccountLink);

    // ---- 4) 退出登录
    await click('.user-trigger');
    await sleep(400);
    await click('.user-menu button.danger');
    await sleep(1500);
    s = await probe();
    check('退出后回到登录页', s.path === '/login' && s.hasLoginForm, `path=${s.path}`);

    // ---- 5) 密码错
    await typeInto('.auth-field:nth-of-type(1) input', 'yun');
    await typeInto('.auth-form .auth-password input', 'wrong-password');
    await click('.auth-submit');
    await sleep(1500);
    s = await probe();
    check('密码错误不进首页且有提示', s.path === '/login' && /不正确/.test(s.body), s.body.slice(0, 80));

    // ---- 6) 深链接未登录 -> 顶到登录页，登录后跳回
    await navigate(BASE + '/account');
    s = await probe();
    check('未登录访问 /account 被顶到登录页', s.path === '/login', `path=${s.path}`);
    await typeInto('.auth-field:nth-of-type(1) input', 'yun');
    await typeInto('.auth-form .auth-password input', 'YunPass123!');
    await click('.auth-submit');
    await sleep(2000);
    s = await probe();
    check('登录后跳回原地址 /account', s.path === '/account', `path=${s.path}`);
    check('账号管理页列出账号', /yun/.test(s.body) && s.hasUserBlock, s.body.slice(0, 80));

    // ---- 7) 用接口造一个普通成员，验证权限
    const backendBase = `http://127.0.0.1:${backendPort}`;
    // 取出当前 cookie 供 httpx 用不方便，改走页面内 fetch
    const memberMade = await page.evalJson(`(async function () {
      var r = await fetch('/api/auth/users', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: 'member1', password: 'Member123!', display_name: '普通同事', role: 'member' }),
      });
      var b = await r.json();
      return { status: r.status, body: b };
    })()`, await ctx());
    check('管理员能新建普通成员', memberMade.status === 200, JSON.stringify(memberMade.body).slice(0, 100));

    // 退出，用成员登录
    await click('.user-trigger');
    await sleep(400);
    await click('.user-menu button.danger');
    await sleep(1500);
    await typeInto('.auth-field:nth-of-type(1) input', 'member1');
    await typeInto('.auth-form .auth-password input', 'Member123!');
    await click('.auth-submit');
    await sleep(2200);
    s = await probe();
    check('普通成员能登录进首页', s.path === '/' && s.hasUserBlock, `path=${s.path}`);
    check('普通成员看不到「账号管理」入口', !s.hasAccountLink);

    // ---- 8) 成员直接访问 /api/auth/users 应 403
    const forbidden = await page.evalJson(`(async function () {
      var r = await fetch('/api/auth/users');
      return { status: r.status };
    })()`, await ctx());
    check('普通成员调管理接口返回 403', forbidden.status === 403, `status=${forbidden.status}`);

    // ---- 截图留证
    try {
      const shot = await page.send('Page.captureScreenshot', { format: 'png' });
      const shotPath = path.join(os.tmpdir(), 'slh_auth_flow.png');
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
    try { frontend?.kill(); } catch {}
    try { backend?.kill(); } catch {}
    try { profile && rmSync(profile, { recursive: true, force: true }); } catch {}
    try { authDir && rmSync(authDir, { recursive: true, force: true }); } catch {}
  }
}

process.exit(await main());
