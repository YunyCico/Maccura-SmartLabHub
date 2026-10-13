/**
 * 验证「汇总提取」页点击「生成汇总表」后不再跳转到数据分析页，
 * 且汇总结果就地显示在汇总提取页内。
 *
 * 一体化：起原生服务 -> 无头 Chrome -> 操作 -> 断言 -> 停服务。
 * 用法：node tools/verify_build_flow.mjs     环境变量：SLH_DEBUG=1
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
const TARGET = `http://127.0.0.1:${PORT}/`;
const PYTHON =
  process.env.SLH_PYTHON ||
  'C:\\Users\\98319\\.workbuddy\\binaries\\python\\envs\\smartlabhub\\Scripts\\python.exe';
const DEBUG = !!process.env.SLH_DEBUG;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

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
  const t = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const res = await fetch(url, { signal: ctl.signal });
    return await res.json();
  } catch {
    return null;
  } finally {
    clearTimeout(t);
  }
}

async function waitJson(url, timeoutMs = 25000) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    const d = await getJson(url);
    if (d) return d;
    await sleep(400);
  }
  return null;
}

async function waitHttp(url, timeoutMs) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    try {
      const ctl = new AbortController();
      const t = setTimeout(() => ctl.abort(), 2500);
      const res = await fetch(url, { signal: ctl.signal });
      clearTimeout(t);
      if (res.ok) return await res.text();
    } catch {}
    await sleep(500);
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
        this.errors.push('console.error: ' + (msg.params.args || []).map((a) => a.value ?? '').join(' '));
      }
    });
  }
  send(method, params = {}, timeoutMs = 25000) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error('timeout ' + method));
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
    if (v === undefined || v === null)
      throw new Error('evaluate empty: ' + JSON.stringify(r).slice(0, 300));
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

async function startServer() {
  if (await waitHttp(TARGET, 800)) {
    console.log('server: already running');
    return null;
  }
  const proc = spawn(PYTHON, ['-c', buildServerCode()], {
    cwd: ROOT,
    env: { ...process.env, NO_PROXY: '*', no_proxy: '*' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  proc.stderr.on('data', (b) => process.stderr.write('[srv!] ' + b));
  const html = await waitHttp(TARGET, 40000);
  if (!html) throw new Error('server did not come up');
  console.log('server: started, html bytes =', html.length);
  return proc;
}

/** 采集当前所在页与结果卡片状态 */
const STATE = `(function () {
  function has(s) { return !!document.querySelector(s); }
  var active = document.querySelector('.page.active');
  var rc = document.querySelector('#result-card');
  return JSON.stringify({
    activePage: active ? active.id : '',
    buildActive: has('#page-build.active'),
    analysisActive: has('#page-analysis.active'),
    resultCardVisible: !!(rc && getComputedStyle(rc).display !== 'none'),
    resultRows: (document.querySelector('#res-count') || {}).textContent || '',
    analysisCard: has('#analysis-card'),
    tipText: (document.querySelector('#run-tip') || {}).textContent || ''
  });
})()`;

async function main() {
  if (!existsSync(CHROME)) return console.log('SKIP chrome missing'), 0;
  if (!existsSync(PYTHON)) return console.log('SKIP python missing'), 0;

  let server = null, chrome = null, profile = null;
  try {
    server = await startServer();
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

    const browser = new Cdp(await openWs(ver.webSocketDebuggerUrl));
    const created = await browser.send('Target.createTarget', { url: 'about:blank' });
    const tid = created.result.targetId;
    let wsUrl = null;
    const dl = Date.now() + 15000;
    while (Date.now() < dl && !wsUrl) {
      const list = (await getJson(`http://127.0.0.1:${port}/json/list`)) || [];
      const hit = list.find((t) => t.id === tid);
      if (hit) wsUrl = hit.webSocketDebuggerUrl;
      if (!wsUrl) await sleep(300);
    }
    if (!wsUrl) throw new Error('no target ws');

    const page = new Cdp(await openWs(wsUrl));
    await page.send('Runtime.enable');
    await page.send('Log.enable');
    await page.send('Page.enable');
    await page.send('Page.navigate', { url: TARGET });

    // 等待应用就绪
    let ok = false;
    const end = Date.now() + 40000;
    while (Date.now() < end && !ok) {
      await sleep(1200);
      try {
        const s = await page.evalJson(
          `(function(){return JSON.stringify({app:!!document.querySelector('#app'),ds:document.querySelectorAll('#page-build .pick').length,t:document.title});})()`,
          15000,
        );
        if (DEBUG) console.log('  ready poll', JSON.stringify(s));
        ok = Boolean(s.app) && !/^127\.0\.0\.1/.test(s.t || '');
      } catch {}
    }
    if (!ok) throw new Error('app not ready');

    // 进入汇总提取页
    await page.send('Runtime.evaluate', {
      expression: `(function(){var a=document.querySelector('.nav a[data-page="build"]'); if(a) a.click(); return 'ok';})()`,
      returnByValue: true,
    });
    await sleep(3000);

    const before = await page.evalJson(STATE);
    console.log('BEFORE ' + JSON.stringify(before));

    // 按真实选择器勾选：#picklist 内的 .dg-check（文件级）、#fieldlist 内的 .of-check（字段级）
    const setup = await page.send('Runtime.evaluate', {
      expression: `(function(){
        var ds = document.querySelectorAll('#picklist input.dg-check');
        var fl = document.querySelectorAll('#fieldlist input.of-check');
        var n1 = 0, n2 = 0;
        ds.forEach(function(c){ if(!c.checked){ c.click(); n1++; } });
        fl.forEach(function(c){ if(!c.checked){ c.click(); n2++; } });
        return JSON.stringify({datasetsFound: ds.length, datasetsTicked: n1,
          fieldsFound: fl.length, fieldsTicked: n2,
          hasRunBtn: !!document.querySelector('#btn-run')});
      })()`,
      returnByValue: true,
    });
    console.log('SETUP ' + (setup?.result?.result?.value || '{}'));

    // 勾选数据表后字段列表是异步重绘的，轮询等它出现
    let fields = 0;
    const endFields = Date.now() + 20000;
    while (Date.now() < endFields) {
      await sleep(1200);
      const s = await page.send('Runtime.evaluate', {
        expression: `(function(){return JSON.stringify({f:document.querySelectorAll('#fieldlist input.of-check').length, pick:document.querySelectorAll('#picklist input.dg-check').length});})()`,
        returnByValue: true,
      });
      const v = JSON.parse(s?.result?.result?.value || '{}');
      fields = v.f || 0;
      if (DEBUG) console.log('  field poll', JSON.stringify(v));
      if (fields > 0) break;
    }
    if (fields > 0) {
      await page.send('Runtime.evaluate', {
        expression: `(function(){
          var fl = document.querySelectorAll('#fieldlist input.of-check');
          var n = 0;
          fl.forEach(function(c){ if(!c.checked){ c.click(); n++; } });
          return JSON.stringify({ticked:n});
        })()`,
        returnByValue: true,
      });
      console.log('FIELDS_TICKED ' + fields);
      await sleep(2000);
    }
    await sleep(2500);

    const readyToRun = await page.evalJson(STATE);
    if (DEBUG) console.log('  before click', JSON.stringify(readyToRun));

    // 点击「生成汇总表」
    await page.send('Runtime.evaluate', {
      expression: `(function(){var b=document.querySelector('#btn-run'); if(b){b.click(); return 'clicked';} return 'no-btn';})()`,
      returnByValue: true,
    });

    // 等待结果卡片出现或超时（大表可能较慢）
    let after = null;
    const end2 = Date.now() + 90000;
    while (Date.now() < end2) {
      await sleep(2000);
      try {
        after = await page.evalJson(STATE);
        if (DEBUG) console.log('  after poll', JSON.stringify(after));
        if (after.resultCardVisible) break;
      } catch {}
    }
    await sleep(1500);
    after = await page.evalJson(STATE);
    console.log('AFTER ' + JSON.stringify(after, null, 2));

    const problems = [];
    if (!after) problems.push('未取到状态');
    else {
      if (!after.resultCardVisible) problems.push('汇总结果卡片未显示');
      if (!after.buildActive) problems.push('生成后离开了汇总提取页（activePage=' + after.activePage + '）');
      if (after.analysisActive) problems.push('生成后跳转到了数据分析页');
      if (after.analysisCard) problems.push('已删除的结果分析面板又出现了');
    }
    if (page.errors.length) problems.push('控制台报错: ' + page.errors.join(' | '));

    if (problems.length) {
      console.log('RESULT: FAIL');
      problems.forEach((p) => console.log('  - ' + p));
      return 1;
    }
    console.log('RESULT: PASS — 生成后停留在汇总提取页，结果就地显示，无跳转');
    console.log('  结果行数:', after.resultRows);
    return 0;
  } catch (e) {
    console.log('FAIL', e && e.message ? e.message : e);
    return 1;
  } finally {
    if (chrome) { chrome.kill(); await sleep(700); }
    if (profile) { try { rmSync(profile, { recursive: true, force: true }); } catch {} }
    if (server) { server.kill(); await sleep(500); console.log('server: stopped'); }
  }
}

process.exit(await main());