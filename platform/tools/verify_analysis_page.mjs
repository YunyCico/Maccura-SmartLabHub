/**
 * 一体化真实验证：起原生 SmartLabHub 服务 -> 无头 Chrome 打开页面 -> 断言 -> 停服务。
 *
 * 为什么一体化：后台启动的进程会在工具会话结束时被回收，
 * 跨命令执行时页面会拿到 chrome-error://，误判成元素缺失。
 *
 * 用法：node tools/verify_analysis_page.mjs
 * 环境变量：SLH_DEBUG=1 打印轮询细节；SLH_KEEP=1 保留服务不停止。
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
const PORT = Number(process.env.SLH_PORT || 8765);
const TARGET = process.env.SLH_TARGET || `http://127.0.0.1:${PORT}/`;

const PYTHON =
  process.env.SLH_PYTHON ||
  'C:\\Users\\98319\\.workbuddy\\binaries\\python\\envs\\smartlabhub\\Scripts\\python.exe';

const DEBUG = !!process.env.SLH_DEBUG;
const KEEP = !!process.env.SLH_KEEP;

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

async function getJson(url, timeoutMs = 4000) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const res = await fetch(url, { signal: ctl.signal });
    return await res.json();
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

async function waitJson(url, timeoutMs = 25000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const data = await getJson(url);
    if (data) return data;
    await new Promise((r) => setTimeout(r, 400));
  }
  return null;
}

async function waitHttp(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const ctl = new AbortController();
      const t = setTimeout(() => ctl.abort(), 2500);
      const res = await fetch(url, { signal: ctl.signal });
      clearTimeout(t);
      if (res.ok) return await res.text();
    } catch {}
    await new Promise((r) => setTimeout(r, 500));
  }
  return null;
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
        this.errors.push(
          'console.error: ' +
            (msg.params.args || []).map((a) => a.value ?? a.description ?? '').join(' '),
        );
      }
    });
  }

  send(method, params = {}, timeoutMs = 25000) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error('timeout: ' + method));
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
    if (v === undefined || v === null) throw new Error('evaluate empty: ' + JSON.stringify(r).slice(0, 300));
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
  function has(sel) { return !!document.querySelector(sel); }
  var lead = document.querySelector('#page-analysis .lead');
  var tatList = document.querySelector('#tat-ds-list');
  var rpList = document.querySelector('#rp-ds-list');
  var body = document.body.innerText || '';
  return JSON.stringify({
    href: location.href,
    title: document.title,
    analysisCard: has('#analysis-card'),
    anPick: has('#an-pick'),
    anBadge: has('#an-badge'),
    anLoading: has('#an-loading'),
    anBody: has('#an-body'),
    pvRun: has('#pv-run'),
    pvOut: has('#pv-out'),
    pvDims: has('#pv-dims'),
    oldTip: body.indexOf('结果分析使用') >= 0,
    resultAnalysisText: body.indexOf('结果分析') >= 0,
    profileTab: body.indexOf('数据概览') >= 0,
    pivotTab: body.indexOf('数据透视') >= 0,
    tatCard: has('#tat-card'),
    tatDsList: has('#tat-ds-list'),
    reportCard: has('#report-card'),
    rpDsList: has('#rp-ds-list'),
    leadText: lead ? lead.innerText : '',
    tatDsCount: tatList ? tatList.children.length : -1,
    rpDsCount: rpList ? rpList.children.length : -1
  });
})()`;

async function startServer() {
  if (await waitHttp(TARGET, 800)) {
    console.log('server: already running on', PORT);
    return null;
  }
  const out = openSyncLog();
  const proc = spawn(
    PYTHON,
    ['-c', buildServerCode()],
    { cwd: ROOT, env: { ...process.env, NO_PROXY: '*', no_proxy: '*' }, stdio: ['ignore', 'pipe', 'pipe'] },
  );
  proc.stdout.on('data', (b) => process.stdout.write('[srv] ' + b));
  proc.stderr.on('data', (b) => process.stderr.write('[srv!] ' + b));
  const html = await waitHttp(TARGET, 40000);
  if (!html) throw new Error('server did not come up on ' + PORT);
  console.log('server: started, html bytes =', html.length);
  void out;
  return proc;
}

function openSyncLog() {
  return null;
}

/** 直接用引擎起原生服务，避免依赖外部已运行的 8000。 */
function buildServerCode() {
  return [
    'import sys, threading',
    `sys.path.insert(0, r"${ROOT}")`,
    'from modules.data_analysis import smartlab_engine as engine',
    'engine.load_state()',
    'engine.load_persisted_results()',
    `srv = engine.ThreadingHTTPServer(("127.0.0.1", ${PORT}), engine.Handler)`,
    'srv.daemon_threads = True',
    'threading.Thread(target=srv.serve_forever, daemon=True).start()',
    'print("LISTENING", flush=True)',
    'import time',
    'time.sleep(3600)',
  ].join('\n');
}

async function main() {
  if (!existsSync(CHROME)) {
    console.log('SKIP chrome not found:', CHROME);
    return 0;
  }
  if (!existsSync(PYTHON)) {
    console.log('SKIP python not found:', PYTHON);
    return 0;
  }

  let server = null;
  let chrome = null;
  let profile = null;
  try {
    server = await startServer();
    if (DEBUG && server) console.log('server pid', server.pid);

    const port = await freePort();
    profile = mkdtempSync(path.join(os.tmpdir(), 'slh_cdp_'));
    const env = { ...process.env };
    for (const k of Object.keys(env)) if (/proxy/i.test(k)) delete env[k];
    env.NO_PROXY = '*';
    env.no_proxy = '*';

    chrome = spawn(
      CHROME,
      [
        `--remote-debugging-port=${port}`,
        `--user-data-dir=${profile}`,
        '--headless=new',
        '--disable-gpu',
        '--no-first-run',
        '--no-default-browser-check',
        '--no-proxy-server',
        '--proxy-bypass-list=*',
        '--window-size=1400,1100',
        'about:blank',
      ],
      { env, stdio: 'ignore' },
    );

    const ver = await waitJson(`http://127.0.0.1:${port}/json/version`);
    if (!ver) throw new Error('devtools unreachable');
    console.log('CHROME', ver.Browser);

    const browserWs = await openWs(ver.webSocketDebuggerUrl);
    const browser = new Cdp(browserWs);
    const created = await browser.send('Target.createTarget', { url: 'about:blank' });
    const targetId = created.result.targetId;

    let wsUrl = null;
    const dl = Date.now() + 15000;
    while (Date.now() < dl && !wsUrl) {
      const list = (await getJson(`http://127.0.0.1:${port}/json/list`)) || [];
      const hit = list.find((t) => t.id === targetId);
      if (hit) wsUrl = hit.webSocketDebuggerUrl;
      if (!wsUrl) await new Promise((r) => setTimeout(r, 300));
    }
    if (!wsUrl) throw new Error('no target websocket');

    const page = new Cdp(await openWs(wsUrl));
    await page.send('Runtime.enable');
    await page.send('Log.enable');
    await page.send('Page.enable');
    await page.send('Page.navigate', { url: TARGET });

    // 轮询直到应用真正渲染，避免固定 sleep 拿到空白页
    let data = null;
    const deadline = Date.now() + 40000;
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 1500));
      try {
        data = await page.evalJson(PROBE, 15000);
      } catch (e) {
        if (DEBUG) console.log('  poll err', e.message);
        continue;
      }
      if (DEBUG) console.log('  poll', JSON.stringify({ href: data.href, title: data.title }));
      if (data.tatCard && data.reportCard && data.tatDsCount >= 0 && !/^127\.0\.0\.1/.test(data.title || '')) break;
    }
    // 留出数据加载时间
    await new Promise((r) => setTimeout(r, 2500));

    // 切到「数据分析」页（列表在该页渲染），再采集最终状态
    try {
      await page.evalJson(`(function(){ return JSON.stringify({ok:true}); })()`);
      const clicked = await page.send('Runtime.evaluate', {
        expression: `(function(){
          var a = document.querySelector('.nav a[data-page="analysis"]');
          if (!a) return 'no-nav';
          a.click();
          return 'clicked';
        })()`,
        returnByValue: true,
      });
      if (DEBUG) console.log('  nav analysis ->', clicked?.result?.result?.value);
      await new Promise((r) => setTimeout(r, 3000));
    } catch (e) {
      if (DEBUG) console.log('  nav error', e.message);
    }

    try {
      data = await page.evalJson(PROBE, 15000);
    } catch {}

    console.log('PAGE_STATE ' + JSON.stringify(data, null, 2));

    const problems = [];
    if (!data || !/127\.0\.0\.1:8765|127\.0\.0\.1:8765/.test(data.href || '') === false && !data.href?.startsWith('http://127.0.0.1')) {
      problems.push('页面未成功加载: ' + (data?.href || 'unknown'));
    }
    if (!data || /127\.0\.0\.1:8765/.test(data.href || '') === false && !data?.href?.startsWith('http://127.0.0.1')) {
      problems.push('页面 URL 异常: ' + (data?.href || 'unknown'));
    }
    if (!data) problems.push('未取到页面状态');
    else {
      for (const k of [
        'analysisCard', 'anPick', 'anBadge', 'anLoading', 'anBody',
        'pvRun', 'pvOut', 'pvDims', 'oldTip', 'resultAnalysisText',
        'profileTab', 'pivotTab',
      ]) {
        if (data[k]) problems.push(`${k} 应已删除，但仍存在`);
      }
      for (const k of ['tatCard', 'tatDsList', 'reportCard', 'rpDsList']) {
        if (!data[k]) problems.push(`${k} 应保留，但缺失`);
      }
      if (data.tatDsCount < 0) problems.push('TAT 数据表列表未渲染');
    }
    if (page.errors.length) problems.push('控制台报错: ' + page.errors.join(' | '));

    if (problems.length) {
      console.log('RESULT: FAIL');
      problems.forEach((p) => console.log('  - ' + p));
      return 1;
    }
    console.log('RESULT: PASS — 两块已删除，TAT/设备负载完好，页面无脚本报错');
    console.log('  lead:', data.leadText);
    console.log('  TAT 数据表条目:', data.tatDsCount, ' 设备负载条目:', data.rpDsCount);
    return 0;
  } catch (err) {
    console.log('FAIL', err && err.message ? err.message : err);
    return 1;
  } finally {
    if (chrome) {
      chrome.kill();
      await new Promise((r) => setTimeout(r, 700));
    }
    if (profile) {
      try {
        rmSync(profile, { recursive: true, force: true });
      } catch {}
    }
    if (server && !KEEP) {
      server.kill();
      await new Promise((r) => setTimeout(r, 500));
      console.log('server: stopped');
    }
  }
}

process.exit(await main());