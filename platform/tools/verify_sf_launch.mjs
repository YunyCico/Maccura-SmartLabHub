/**
 * 真实验证 Salesforce 免登跳转：
 * 首页 Salesforce 板块点「查看全部 ↗」-> 浏览器应该**直接进 Salesforce 原生页**，
 * 而不是被踢回登录页；并且浏览器要在 sfcrmproducts.cn 上拿到 sid cookie。
 *
 * 用法：node platform/tools/verify_sf_launch.mjs
 *
 * ⚠️ 这个脚本会连真实的 Salesforce（只读地打开一个页面），
 *    并且后端用真实 integrations.json（需要真 sid），所以**不隔离数据目录**。
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
    this.errors = [];
    this.contexts = new Map();
    ws.addEventListener('message', (event) => {
      const message = JSON.parse(event.data);
      if (message.id && this.pending.has(message.id)) {
        const pending = this.pending.get(message.id);
        this.pending.delete(message.id);
        pending.resolve(message);
        return;
      }
      if (message.method === 'Runtime.executionContextCreated') {
        const context = message.params.context;
        this.contexts.set(context.id, context);
      }
      if (message.method === 'Runtime.exceptionThrown') {
        const details = message.params?.exceptionDetails || {};
        this.errors.push(details.exception?.description || details.text || 'runtime exception');
      }
    });
  }

  send(method, params = {}, timeoutMs = 45000) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`timeout: ${method}`));
      }, timeoutMs);
      this.pending.set(id, {
        resolve: (message) => {
          clearTimeout(timer);
          resolve(message);
        },
      });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }

  async evaluate(expression, contextId) {
    const params = { expression, returnByValue: true, awaitPromise: true };
    if (contextId) params.contextId = contextId;
    const response = await this.send('Runtime.evaluate', params);
    const value = response?.result?.result?.value;
    if (value === undefined) throw new Error(`evaluate failed: ${JSON.stringify(response).slice(0, 400)}`);
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
  env.NO_PROXY = '*';
  env.no_proxy = '*';

  let backend = null;
  let frontend = null;
  let chrome = null;
  let profile = null;
  try {
    const backendPort = await freePort();
    const frontendPort = await freePort();
    backend = spawn(PYTHON, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(backendPort), '--app-dir', 'platform/backend'], {
      cwd: ROOT, env, stdio: ['ignore', 'pipe', 'pipe'],
    });
    backend.stderr.on('data', (chunk) => {
      const text = chunk.toString();
      if (!/ConnectionAbortedError|Traceback|File "\/".*, line/.test(text)) process.stderr.write('[api!] ' + text);
    });
    if (!(await waitHttp(`http://127.0.0.1:${backendPort}/api/health`, 90000))) throw new Error('backend did not come up');

    frontend = spawn(process.execPath, [path.join(ROOT, 'home', 'node_modules', 'vite', 'bin', 'vite.js'), '--host', '127.0.0.1', '--port', String(frontendPort), '--strictPort'], {
      cwd: path.join(ROOT, 'home'),
      env: { ...env, SLH_API_TARGET: `http://127.0.0.1:${backendPort}` },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    frontend.stdout.on('data', (chunk) => process.stdout.write('[vite] ' + chunk));
    frontend.stderr.on('data', (chunk) => process.stderr.write('[vite!] ' + chunk));
    if (!(await waitHttp(`http://127.0.0.1:${frontendPort}/`, 90000))) throw new Error('frontend did not come up');

    const debugPort = await freePort();
    profile = mkdtempSync(path.join(os.tmpdir(), 'slh_sf_launch_'));
    chrome = spawn(CHROME, [
      `--remote-debugging-port=${debugPort}`,
      `--user-data-dir=${profile}`,
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
    const targetId = created.result.targetId;
    let targetInfo = null;
    const targetEnd = Date.now() + 15000;
    while (Date.now() < targetEnd && !targetInfo) {
      const targets = await fetch(`http://127.0.0.1:${debugPort}/json/list`).then((r) => r.json()).catch(() => []);
      targetInfo = targets.find((item) => item.id === targetId);
      if (!targetInfo) await sleep(300);
    }
    if (!targetInfo) throw new Error('page target unavailable');

    const page = new Cdp(await openWs(targetInfo.webSocketDebuggerUrl));
    await page.send('Runtime.enable');
    await page.send('Page.enable');
    await page.send('Network.enable');
    await page.send('Page.navigate', { url: `http://127.0.0.1:${frontendPort}/` });

    let ctxId = null;
    const readyEnd = Date.now() + 60000;
    while (Date.now() < readyEnd && !ctxId) {
      await sleep(800);
      ctxId = [...page.contexts.values()].find((c) => c.origin.includes(`127.0.0.1:${frontendPort}`))?.id || null;
    }
    if (!ctxId) throw new Error('Vue 页面未建立执行上下文');

    let section = null;
    const sectionEnd = Date.now() + 120000;
    while (Date.now() < sectionEnd && !section) {
      await sleep(1000);
      try {
        section = await page.evalJson(`(function () {
          var cards = [...document.querySelectorAll('.sf-card')];
          if (!cards.length) return null;
          // 必须等数据真的加载出来（不是只有空壳卡片）
          var text = document.body.innerText || '';
          var connected = text.indexOf('Salesforce 已连接') >= 0 || text.indexOf('已连接') >= 0;
          var items = document.querySelectorAll('.sf-list-item').length;
          if (!connected && !items) return { cardCount: cards.length, ready: false };
          var buttons = [...document.querySelectorAll('.sf-quick-action')].map(function (b) { return b.textContent.trim(); });
          return { cardCount: cards.length, ready: true, items: items, buttons: buttons };
        })()`, ctxId);
      } catch {}
      if (section && !section.ready) section = null;
    }
    console.log('SF_SECTION', JSON.stringify(section));
    if (!section) throw new Error('Salesforce 板块数据未加载出来');

    const clicked = await page.evalJson(`(function () {
      var btn = [...document.querySelectorAll('.sf-quick-action')].find(function (b) {
        return b.textContent.indexOf('查看全部') >= 0;
      });
      if (!btn) return { clicked: false };
      btn.click();
      return { clicked: true, label: btn.textContent.trim() };
    })()`, ctxId);
    console.log('CLICK', JSON.stringify(clicked));
    if (!clicked.clicked) throw new Error('没找到「查看全部」按钮');

    // 弹窗被拦时退化为**当前页跳转**，所以要盯住那个正在跳转的标签页。
    // 跳转链：/api/.../launch?t=xxx -> frontdoor.jsp -> contentDoor -> 最终页
    let navTarget = null;
    let finalUrl = '';
    const seen = [];
    const navEnd = Date.now() + 120000;
    while (Date.now() < navEnd) {
      await sleep(800);
      const targets = await fetch(`http://127.0.0.1:${debugPort}/json/list`).then((r) => r.json()).catch(() => []);
      const pages = targets.filter((t) => t.type === 'page');
      for (const t of pages) {
        const sig = (t.url || '').slice(0, 90);
        if (sig && !seen.includes(sig)) seen.push(sig);
      }
      const onSf = pages.find((t) => /sfcrmproducts|sfcrmapps/.test(t.url || ''));
      if (onSf) {
        navTarget = onSf;
        finalUrl = onSf.url || '';
        break;
      }
      const navigating = pages.find(
        (t) => t.url && t.url !== 'about:blank' && !t.url.startsWith('http://127.0.0.1'),
      );
      if (navigating) {
        navTarget = navigating;
        finalUrl = navigating.url || '';
      }
    }
    console.log('TARGETS_SEEN', JSON.stringify(seen));
    if (!navTarget) throw new Error('没有跳转到 Salesforce');
    console.log('NAV_TARGET', JSON.stringify({ id: navTarget.id, url: (navTarget.url || '').slice(0, 160) }));

    // 等最终落地（contentDoor 里还有一次 JS 跳转）
    const settleEnd = Date.now() + 60000;
    while (Date.now() < settleEnd) {
      await sleep(1000);
      const t = await fetch(`http://127.0.0.1:${debugPort}/json/list`).then((r) => r.json()).catch(() => []);
      const cur = t.find((x) => x.id === navTarget.id);
      if (cur?.url) {
        finalUrl = cur.url;
        navTarget = cur;
      }
      if (/lightning|my\.sfcrmproducts|contentDoor/.test(finalUrl) && !/login/i.test(finalUrl)) break;
    }
    console.log('FINAL_URL', finalUrl.slice(0, 200));

    // 重新附着（导航后原来的执行上下文已失效）
    const sfPage = new Cdp(await openWs(navTarget.webSocketDebuggerUrl));
    await sfPage.send('Page.enable');
    await sfPage.send('Network.enable');
    // 先禁再启，强制把当前页的执行上下文重新推一遍
    // （附着时页面可能已经导航完，漏了 executionContextCreated 事件）
    await sfPage.send('Runtime.enable');
    await sfPage.send('Runtime.disable');
    await sfPage.send('Runtime.enable');
    let sfCtx = null;
    const sfCtxEnd = Date.now() + 30000;
    while (Date.now() < sfCtxEnd && !sfCtx) {
      await sleep(700);
      sfCtx = [...sfPage.contexts.values()].find((c) => /sfcrmproducts|sfcrmapps/.test(c.origin))?.id || null;
    }

    let body = '';
    // 页面还在连续跳转（frontdoor -> contentDoor -> 最终页），上下文会失效，重试几次
    for (let attempt = 0; attempt < 12; attempt++) {
      await sleep(1500);
      const cur = await fetch(`http://127.0.0.1:${debugPort}/json/list`).then((r) => r.json()).catch(() => []);
      const now = cur.find((x) => x.id === navTarget.id);
      if (now?.url) {
        finalUrl = now.url;
        navTarget = now;
      }
      try {
        sfCtx = [...sfPage.contexts.values()].find((c) => /sfcrmproducts|sfcrmapps/.test(c.origin))?.id || null;
        // Lightning 应用挂在 shadow DOM 里，body.innerText 往往是空的，
        // 所以改用结构判定：有没有密码框 = 是不是登录页。
        body = await sfPage.evaluate(`(function () {
          var pw = document.querySelectorAll('input[type="password"]').length;
          var app = document.querySelectorAll('lightning-app, .slds-app, [id^="aura"], one-app-home, .force-app').length;
          return JSON.stringify({
            title: document.title || '',
            ready: document.readyState || '',
            passwordInputs: pw,
            appRoots: app,
            hasSfText: /迈克|销售|任务|日程|周计划|客户/.test(document.documentElement.innerHTML || ''),
          });
        })()`, sfCtx || undefined);
        if (body && body.includes('"ready"') && body.length > 30) break;
      } catch {
        // 上下文在连续跳转中会失效，重试即可
      }
    }
    const probe = (() => {
      try { return JSON.parse(body); } catch { return {}; }
    })();
    console.log('PAGE_PROBE', JSON.stringify(probe));
    console.log('FINAL_URL', finalUrl.slice(0, 200));

    // 截图存证
    try {
      const shot = await sfPage.send('Page.captureScreenshot', { format: 'png' });
      const shotPath = path.join(os.tmpdir(), 'slh_sf_launch.png');
      writeFileSync(shotPath, Buffer.from(shot.result.data, 'base64'));
      console.log('SCREENSHOT', shotPath);
    } catch (e) {
      console.log('SCREENSHOT_SKIPPED', String(e.message || e).slice(0, 120));
    }

    const cookies = await sfPage.send('Network.getAllCookies');
    const sfCookies = (cookies.result.cookies || []).filter((c) => /sfcrmproducts/.test(c.domain || ''));
    console.log('COOKIES', JSON.stringify(sfCookies.map((c) => `${c.domain}:${c.name}`)));

    if ((probe.passwordInputs || 0) > 0) throw new Error('页面里有密码输入框 —— 仍然是登录页，免登没生效');
    if (/login\.sfcrmproducts|login$|\/login\//.test(finalUrl)) throw new Error('落到了登录域名: ' + finalUrl);
    if (!sfCookies.some((c) => c.name === 'sid')) throw new Error('浏览器没有拿到 sid cookie');
    if (!/lightning/.test(finalUrl)) throw new Error('最终没落在 Lightning 界面: ' + finalUrl);
    if (/lightning\/404/.test(finalUrl)) throw new Error('落到了 404 页: ' + finalUrl);

    console.log('RESULT: PASS — 点击「查看全部」直接进 Salesforce，已免登');
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
