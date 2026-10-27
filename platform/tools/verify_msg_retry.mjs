/**
 * 验证「后端暂时不可用 → 前端自动重试 → 后端起来后错误条自动消失」。
 *
 * 用法：node platform/tools/verify_msg_retry.mjs
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

async function main() {
  if (!existsSync(CHROME)) return console.log('SKIP chrome missing'), 0;
  if (!existsSync(PYTHON)) return console.log('SKIP python missing'), 0;

  const env = { ...process.env, NO_PROXY: '*', no_proxy: '*' };
  for (const key of Object.keys(env)) if (/proxy/i.test(key)) delete env[key];

  let backend = null; let frontend = null; let chrome = null; let profile = null;
  try {
    const backendPort = await freePort();
    const frontendPort = await freePort();

    // ★ 先只起前端 —— 故意让后端缺席，模拟「平台刚启动/后端正在重启」
    frontend = spawn(process.execPath, [path.join(ROOT, 'home', 'node_modules', 'vite', 'bin', 'vite.js'), '--host', '127.0.0.1', '--port', String(frontendPort), '--strictPort'], {
      cwd: path.join(ROOT, 'home'),
      env: { ...env, SLH_API_TARGET: `http://127.0.0.1:${backendPort}` },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    if (!(await waitHttp(`http://127.0.0.1:${frontendPort}/`, 90000))) throw new Error('frontend did not come up');
    console.log('前端已起（后端故意缺席）');

    const debugPort = await freePort();
    profile = mkdtempSync(path.join(os.tmpdir(), 'slh_msg_retry_'));
    chrome = spawn(CHROME, [
      `--remote-debugging-port=${debugPort}`, `--user-data-dir=${profile}`,
      '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
      '--no-proxy-server', '--proxy-bypass-list=*', '--window-size=1500,1100', 'about:blank',
    ], { env, stdio: 'ignore' });

    let version = null;
    const vEnd = Date.now() + 25000;
    while (Date.now() < vEnd && !version) {
      version = await (await fetch(`http://127.0.0.1:${debugPort}/json/version`, { signal: AbortSignal.timeout(3000) }).then((r) => r.json()).catch(() => null));
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
    await page.send('Page.navigate', { url: `http://127.0.0.1:${frontendPort}/` });

    let ctx = null;
    const rEnd = Date.now() + 60000;
    while (Date.now() < rEnd && !ctx) {
      await sleep(800);
      ctx = [...page.contexts.values()].find((c) => c.origin.includes(`127.0.0.1:${frontendPort}`))?.id || null;
    }
    if (!ctx) throw new Error('Vue 页面未建立上下文');

    // 1) 后端缺席时，错误条应出现且文案是「正在自动重试」
    let banner = null;
    const bEnd = Date.now() + 45000;
    let printed = false;
    while (Date.now() < bEnd && !banner) {
      await sleep(1000);
      const dump = await page.evalJson(`(function () {
        var bars = [...document.querySelectorAll('.notice-bar')].map(function (e) {
          return { cls: e.className, text: e.textContent.replace(/\\s+/g, ' ').trim() };
        });
        return {
          bars: bars,
          body: (document.body ? document.body.innerText : '').replace(/\\s+/g, ' ').slice(0, 400),
          hasApp: !!document.querySelector('#app') && document.querySelector('#app').children.length > 0,
        };
      })()`, ctx).catch((e) => ({ err: String(e.message || e) }));
      if (!printed) {
        // 直接在页面里 fetch 一次，看这个请求在浏览器里到底是什么结果
        const probe = await page.evalJson(`(async function () {
          try {
            var r = await fetch('/api/messages?limit=50');
            var t = await r.text();
            return { status: r.status, ok: r.ok, type: r.headers.get('content-type'), head: t.slice(0, 120) };
          } catch (e) { return { error: String(e) }; }
        })()`, ctx).catch((e) => ({ err: String(e.message || e) }));
        console.log('  页面内 fetch 结果:', JSON.stringify(probe));
        console.log('  首次诊断:', JSON.stringify(dump).slice(0, 500));
        printed = true;
      }
      const warn = (dump.bars || []).find((b) => b.cls.includes('warning') && b.text.includes('自动重试'));
      if (warn) banner = warn.text;
    }
    console.log('后端缺席时的提示:', JSON.stringify(banner));
    if (!banner) throw new Error('后端缺席时没有出现「正在自动重试」提示条');
    if (!banner.includes('自动重试')) throw new Error('提示文案没说明会自动重试: ' + banner);

    // 2) 后端起来后，错误条应自动消失
    backend = spawn(PYTHON, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(backendPort), '--app-dir', 'platform/backend'], {
      cwd: ROOT, env, stdio: ['ignore', 'pipe', 'pipe'],
    });
    if (!(await waitHttp(`http://127.0.0.1:${backendPort}/api/health`, 90000))) throw new Error('backend did not come up');
    console.log('后端已起，等待自愈…');

    let cleared = false;
    const cEnd = Date.now() + 60000;
    while (Date.now() < cEnd && !cleared) {
      await sleep(1000);
      cleared = await page.evalJson(`(function () {
        var bars = [...document.querySelectorAll('.notice-bar.warning')];
        return !bars.some(function (b) { return b.textContent.indexOf('自动重试') >= 0; });
      })()`, ctx).catch(() => false);
    }
    const after = await page.evalJson(`(function () {
      var bars = [...document.querySelectorAll('.notice-bar.warning')].map(function (b) {
        return b.textContent.replace(/\\s+/g, ' ').trim();
      });
      return {
        bars: bars,
        pending: (document.querySelector('.todo-count, .nc-count, .todo-heading span') || {}).textContent || '',
        body: (document.body ? document.body.innerText : '').replace(/\\s+/g, ' ').slice(0, 160),
      };
    })()`, ctx).catch(() => ({}));
    console.log('后端起来后:', JSON.stringify(after));
    if (!cleared) throw new Error('后端恢复后「正在自动重试」提示条没有自动消失');

    console.log('RESULT: PASS — 后端不可用时提示且自动重试，后端恢复后提示自动消失');
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
