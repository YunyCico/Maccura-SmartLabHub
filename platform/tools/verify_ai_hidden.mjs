/**
 * 验证「AI 助手」已隐藏、但其他板块不受影响。
 *
 * 用法：node platform/tools/verify_ai_hidden.mjs [引擎地址]
 * 默认 http://127.0.0.1:8765/
 */
import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';

const TARGET = process.argv[2] || process.env.SLH_EMBED_URL || 'http://127.0.0.1:8765/';
const CHROME = path.join(process.env.LOCALAPPDATA || '', 'Google/Chrome/Application/chrome.exe');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

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
  console.log('目标:', TARGET);

  const env = { ...process.env, NO_PROXY: '*', no_proxy: '*' };
  for (const key of Object.keys(env)) if (/proxy/i.test(key)) delete env[key];

  let chrome = null; let profile = null;
  try {
    const debugPort = await freePort();
    profile = mkdtempSync(path.join(os.tmpdir(), 'slh_ai_hidden_'));
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
    await page.send('Page.navigate', { url: TARGET });
    await sleep(3000);

    let ctx = null;
    for (let i = 0; i < 40; i++) {
      ctx = [...page.contexts.values()].find((c) => c.origin.startsWith('http'))?.id;
      if (ctx) break;
      await sleep(300);
    }
    if (!ctx) throw new Error('页面未建立执行上下文');

    // 1) AI 菜单项不在了
    const nav = await page.evalJson(`(function () {
      var links = [...document.querySelectorAll('.nav a[data-key]')];
      return {
        keys: links.map(function (a) { return a.dataset.key; }),
        labels: links.map(function (a) { return a.textContent.trim(); }),
      };
    })()`, ctx);
    check('左侧菜单里没有 AI 助手', !nav.keys.includes('ai'), '菜单=' + nav.keys.join('/'));
    check('其他板块都还在（>=6 个）', nav.keys.length >= 6, `共 ${nav.keys.length} 个`);

    // 2) AI 页面始终不可见
    const aiVisible = await page.evalJson(`(function () {
      var s = document.getElementById('page-ai');
      if (!s) return { state: 'missing' };
      return { state: s.classList.contains('active') ? 'visible' : 'hidden' };
    })()`, ctx);
    check('AI 页面未被激活', aiVisible.state === 'hidden', `状态=${aiVisible.state}`);

    // 3) 强行调 nav('ai') 也进不去
    const forced = await page.evalJson(`(function () {
      try { nav('ai'); } catch (e) { return 'error:' + e.message; }
      var s = document.getElementById('page-ai');
      var actives = [...document.querySelectorAll('.page.active')].map(function (x) { return x.id; });
      return { aiActive: s ? s.classList.contains('active') : null, actives: actives };
    })()`, ctx);
    check('脚本强行 nav("ai") 也被拦住', forced.aiActive === false, JSON.stringify(forced));

    // 4) 正常板块还能切（点「数据源管理」）
    const switched = await page.evalJson(`(function () {
      var link = document.querySelector('.nav a[data-key="sources"]');
      if (!link) return { ok: false };
      link.click();
      var s = document.getElementById('page-sources');
      return { ok: !!s && s.classList.contains('active') };
    })()`, ctx);
    check('点「数据源管理」仍能正常切换', switched.ok === true);

    // 5) 再切汇总提取
    const switched2 = await page.evalJson(`(function () {
      var link = document.querySelector('.nav a[data-key="build"]');
      if (!link) return { ok: false };
      link.click();
      var s = document.getElementById('page-build');
      return { ok: !!s && s.classList.contains('active') };
    })()`, ctx);
    check('点「汇总提取」仍能正常切换', switched2.ok === true);

    // 6) 没有 JS 报错
    const errs = await page.evalJson(`(function () {
      var box = document.getElementById('error-card');
      return {
        selfCheck: window.__unboundButtons || [],
        errorCardVisible: box ? getComputedStyle(box).display !== 'none' : false,
      };
    })()`, ctx);
    check('自检没有未绑定按钮', errs.selfCheck.length === 0, JSON.stringify(errs.selfCheck));
    check('页面没有弹出错误卡片', errs.errorCardVisible === false);

    // 截图
    try {
      await page.evalJson(`(function(){ document.querySelector('.nav a[data-key="home"]').click(); return true; })()`, ctx);
      await sleep(800);
      const shot = await page.send('Page.captureScreenshot', { format: 'png' });
      const shotPath = path.join(os.tmpdir(), 'slh_ai_hidden.png');
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
    try { profile && rmSync(profile, { recursive: true, force: true }); } catch {}
  }
}

process.exit(await main());
