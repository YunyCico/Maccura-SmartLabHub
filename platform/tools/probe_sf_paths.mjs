/**
 * 实测各种 Salesforce 目标路径能不能正确落地（真实浏览器）。
 *
 * 背景：经典版路径（/007/o、/00T/e）在 Lightning 界面下会跳到
 * 「页面不存在」—— 截图已证实。必须用 Lightning 路由。
 *
 * 用法：node platform/tools/probe_sf_paths.mjs
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

const CANDIDATES = [
  ['经典 商机列表', '/007/o'],
  ['经典 任务列表', '/00T/e'],
  ['经典 任务新建', '/00T/e?action=New'],
  ['经典 日程列表', '/00U/e'],
  ['Lightning 任务列表', '/lightning/o/Task/list'],
  ['Lightning 任务新建', '/lightning/o/Task/new'],
  ['Lightning 日程列表', '/lightning/o/Event/list'],
  ['Lightning 日程新建', '/lightning/o/Event/new'],
  ['Lightning 商机列表', '/lightning/o/Opportunity/list'],
  ['Lightning 周计划列表', '/lightning/o/Weekly_Plan__c/list'],
  ['Lightning 审批项列表', '/lightning/o/ProcessInstanceWorkitem/list'],
  ['Lightning 首页', '/lightning/page/home'],
  ['经典审批页', '/ui/approval/ui/ApprovalRequestListPage.apexp'],
  ['经典审批页', '/ui/approval/ui/ApprovalRequestListPage.apexp'],
  ['经典 任务详情', '/00TC8000013c0JNMAY'],
  ['Lightning 任务详情', '/lightning/r/Task/00TC8000013c0JNMAY/view'],
  ['Lightning 周计划新建', '/lightning/o/Weekly_Plan__c/new'],
  ['Lightning 周计划详情', '/lightning/r/Weekly_Plan__c/a0yC800000RMrKfIAL/view'],
];

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
      if (!/Traceback|ConnectionAborted|File "/.test(t)) process.stderr.write('[api!] ' + t);
    });
    if (!(await waitHttp(`http://127.0.0.1:${backendPort}/api/health`, 90000))) throw new Error('backend did not come up');

    frontend = spawn(process.execPath, [path.join(ROOT, 'home', 'node_modules', 'vite', 'bin', 'vite.js'), '--host', '127.0.0.1', '--port', String(frontendPort), '--strictPort'], {
      cwd: path.join(ROOT, 'home'),
      env: { ...env, SLH_API_TARGET: `http://127.0.0.1:${backendPort}` },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    if (!(await waitHttp(`http://127.0.0.1:${frontendPort}/`, 90000))) throw new Error('frontend did not come up');

    const debugPort = await freePort();
    profile = mkdtempSync(path.join(os.tmpdir(), 'slh_sf_paths_'));
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
    const results = [];

    for (const [label, sfPath] of CANDIDATES) {
      // 每次开一个新标签页，用完关掉
      const created = await browser.send('Target.createTarget', { url: 'about:blank' });
      const tid = created.result.targetId;
      let info = null;
      const end = Date.now() + 10000;
      while (Date.now() < end && !info) {
        const list = await fetch(`http://127.0.0.1:${debugPort}/json/list`).then((r) => r.json()).catch(() => []);
        info = list.find((t) => t.id === tid);
        if (!info) await sleep(250);
      }
      if (!info) { results.push([label, sfPath, '打不开', '', '']); continue; }

      // 先在后端换一个免登令牌
      const tokenRes = await fetch(`http://127.0.0.1:${backendPort}/api/integrations/salesforce/launch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: sfPath }),
      }).then((r) => r.json()).catch(() => null);
      if (!tokenRes?.token) { results.push([label, sfPath, '令牌失败', '', '']); continue; }

      const page = new Cdp(await openWs(info.webSocketDebuggerUrl));
      await page.send('Runtime.enable');
      await page.send('Page.enable');
      await page.send('Page.navigate', { url: `http://127.0.0.1:${frontendPort}/api/integrations/salesforce/launch?t=${encodeURIComponent(tokenRes.token)}` });

      // 等落地
      let finalUrl = '';
      const settle = Date.now() + 35000;
      while (Date.now() < settle) {
        await sleep(1200);
        const list = await fetch(`http://127.0.0.1:${debugPort}/json/list`).then((r) => r.json()).catch(() => []);
        const cur = list.find((t) => t.id === tid);
        if (cur?.url) finalUrl = cur.url;
        if (/lightning|sfcrmproducts/.test(finalUrl) && !/frontdoor|contentDoor/.test(finalUrl)) break;
      }
      await sleep(2500);

      let text = '';
      try {
        const ctx = [...page.contexts.values()].find((c) => /sfcrmproducts|sfcrmapps/.test(c.origin))?.id;
        text = await page.evaluate('document.body ? document.body.innerText : ""', ctx);
      } catch (e) {
        text = `（读不到正文：${String(e.message || e).slice(0, 60)}）`;
      }
      const notFound = /页面不存在|Page not found|找不到页面|URLNoMatch/.test(text);
      const isLogin = /Log In to Salesforce|Salesforce login|验证您的身份/.test(text);
      results.push([
        label, sfPath,
        isLogin ? '登录页' : (notFound ? '页面不存在' : 'OK'),
        finalUrl.replace(/https:\/\/[^/]+/, '').slice(0, 70),
        text.replace(/\s+/g, ' ').slice(0, 70),
      ]);
      await browser.send('Target.closeTarget', { targetId: tid }).catch(() => {});
    }

    console.log('\n===== 路径实测结果 =====');
    for (const row of results) {
      console.log(`${String(row[2]).padEnd(12)} | ${row[0].padEnd(20)} | ${row[1].padEnd(46)} | ${row[3]}`);
      if (row[2] !== 'OK') console.log(`${' '.repeat(13)}└─ 正文: ${row[4]}`);
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
