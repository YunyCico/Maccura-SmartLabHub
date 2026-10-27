/**
 * 验证首页快捷导航 + 改名（2026-10-27）：
 *   1. SmartLabHub 下方出现 3 个快捷按钮（Notification Center / Work Modules / Report）
 *   2. 点击按钮后页面滚动到对应区域（scrollIntoView 生效）
 *   3. 「部门工作模块」已改名为 Work Modules，「分析报表」已改名为 Report
 *
 * 用法：node platform/tools/verify_quick_nav.mjs
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
    profile = mkdtempSync(path.join(os.tmpdir(), 'slh_qnav_'));
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
        const c = [...page.contexts.values()].find((x) => x.origin.includes(':5173'))?.id;
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
    async function clickAt(selector) {
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
      await sleep(1000);
    }

    // ---- 登录
    await page.send('Page.navigate', { url: `${FRONTEND}/login` });
    await sleep(3000);
    await typeInto('.auth-field:nth-of-type(1) input', USERNAME);
    await typeInto('.auth-form .auth-password input', PASSWORD);
    await clickAt('.auth-submit');
    await sleep(4000);
    const logged = await page.evalJson(`(function () { return { path: location.pathname }; })()`, await ctx());
    check(`用 ${USERNAME} 登录成功`, logged.path === '/', JSON.stringify(logged));
    if (logged.path !== '/') throw new Error('登录未成功');

    // ---- 断言 1：三个快捷按钮都在，且文本正确
    const nav = await page.evalJson(`(function () {
      var btns = Array.prototype.slice.call(document.querySelectorAll('.quick-nav-item'));
      return {
        count: btns.length,
        labels: btns.map(function (b) { return (b.textContent || '').trim(); }),
        visible: btns.map(function (b) { return b.offsetParent !== null; }),
      };
    })()`, await ctx());
    check('快捷按钮共 3 个', nav.count === 3, `count=${nav.count} labels=${JSON.stringify(nav.labels)}`);
    const expected = ['Notification Center', 'Work Modules', 'Report'];
    const labelsOk = nav.count === 3 && expected.every((t) => nav.labels.includes(t));
    check('按钮文本为 Notification Center / Work Modules / Report', labelsOk, JSON.stringify(nav.labels));
    check('快捷按钮可见', nav.visible.every(Boolean), JSON.stringify(nav.visible));

    // ---- 断言 2：改名生效
    const headings = await page.evalJson(`(function () {
      var body = document.body.innerText || '';
      var hs = Array.prototype.slice.call(document.querySelectorAll('h3'))
        .map(function (h) { return (h.textContent || '').trim(); });
      return {
        hs: hs,
        hasWorkModules: hs.indexOf('Work Modules') >= 0,
        hasReport: hs.indexOf('Report') >= 0,
        oldModule: hs.indexOf('部门工作模块') >= 0,
        oldReport: hs.indexOf('分析报表') >= 0,
      };
    })()`, await ctx());
    check('「部门工作模块」已改名为 Work Modules', headings.hasWorkModules && !headings.oldModule,
      `h3 列表=${JSON.stringify(headings.hs)}`);
    check('「分析报表」已改名为 Report', headings.hasReport && !headings.oldReport, '');

    // ---- 断言 3：锚点 id 都存在
    const anchors = await page.evalJson(`(function () {
      return ['sec-notifications', 'sec-work-modules', 'sec-report'].map(function (id) {
        var el = document.getElementById(id);
        return { id: id, exists: !!el, top: el ? Math.round(el.getBoundingClientRect().top + window.scrollY) : null };
      });
    })()`, await ctx());
    check('三个锚点 id 都存在', anchors.every((a) => a.exists), JSON.stringify(anchors));

    // ---- 断言 4：点击「Report」按钮，页面滚到 Report 区域
    const before = await page.evalJson(`(function () { return Math.round(window.scrollY); })()`, await ctx());
    await clickAt('.quick-nav-item:nth-of-type(3)');
    // smooth 滚动需要时间
    await sleep(2000);
    const after = await page.evalJson(`(function () {
      var el = document.getElementById('sec-report');
      var rect = el ? el.getBoundingClientRect() : null;
      return {
        scrollY: Math.round(window.scrollY),
        sectionTop: rect ? Math.round(rect.top) : null,
      };
    })()`, await ctx());
    check('点击 Report 按钮后页面发生滚动', after.scrollY > before + 100,
      `before=${before} after=${after.scrollY}`);
    check('Report 区域滚到视口内（顶部接近或越过视口顶）',
      after.sectionTop !== null && after.sectionTop < 300,
      `sectionTop=${after.sectionTop}`);

    // ---- 断言 5：点击「Notification Center」能滚回上面
    await clickAt('.quick-nav-item:nth-of-type(1)');
    await sleep(2000);
    const back = await page.evalJson(`(function () {
      var el = document.getElementById('sec-notifications');
      var rect = el ? el.getBoundingClientRect() : null;
      return { scrollY: Math.round(window.scrollY), top: rect ? Math.round(rect.top) : null };
    })()`, await ctx());
    check('点击 Notification Center 能滚回顶部区域',
      back.top !== null && back.top > -300 && back.top < 300,
      `scrollY=${back.scrollY} top=${back.top}`);

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
