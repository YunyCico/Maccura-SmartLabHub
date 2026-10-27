/**
 * 验证首页两处改动（2026-10-27）：
 *   1. 「工作概况」标题区已被删除
 *   2. Notifications Center 里每个消息区域最多显示 6 条，其余可滚动查看
 *
 * 真实 Chrome + 真实登录，断言基于实际渲染结果。
 *
 * 用法：node platform/tools/verify_home_scroll.mjs
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
    profile = mkdtempSync(path.join(os.tmpdir(), 'slh_scroll_'));
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

    // ---- 登录
    await page.send('Page.navigate', { url: `${FRONTEND}/login` });
    await sleep(3000);
    await typeInto('.auth-field:nth-of-type(1) input', USERNAME);
    await typeInto('.auth-form .auth-password input', PASSWORD);
    await click('.auth-submit');
    await sleep(4000);
    const logged = await page.evalJson(`(function () {
      return { path: location.pathname, hasShell: !!document.querySelector('.brand-bar') };
    })()`, await ctx());
    check(`用 ${USERNAME} 登录成功`, logged.path === '/' && logged.hasShell, JSON.stringify(logged));
    if (logged.path !== '/') throw new Error('登录未成功');

    // 等消息加载
    await sleep(4000);

    // ---- 断言 1：「工作概况」已删除
    const overview = await page.evalJson(`(function () {
      var body = document.body.innerText || '';
      return {
        hasIntroRow: !!document.querySelector('.intro-row'),
        hasStatusPill: !!document.querySelector('.status-pill'),
        hasHeading: body.indexOf('工作概况') >= 0,
        // SmartLabHub 大标题（来自 App.vue 的 topbar）应当还在
        hasSmartLabHub: body.indexOf('SmartLabHub') >= 0,
        // Notifications Center 必须在
        hasNotifications: body.indexOf('Notifications Center') >= 0,
      };
    })()`, await ctx());
    check('「工作概况」标题区已删除', !overview.hasIntroRow && !overview.hasStatusPill && !overview.hasHeading,
      JSON.stringify(overview));
    check('页面主体仍在（SmartLabHub 标题 + Notifications Center）',
      overview.hasSmartLabHub && overview.hasNotifications, JSON.stringify(overview));

    // ---- 断言 2：每个消息区域最多显示 6 条
    // 检查所有挂了指令的列表容器
    const lists = await page.evalJson(`(function () {
      var sels = ['.todo-list', '.remind-list', '.sf-list'];
      var out = [];
      sels.forEach(function (sel) {
        Array.prototype.slice.call(document.querySelectorAll(sel)).forEach(function (el, i) {
          var kids = Array.prototype.slice.call(el.children);
          var cs = getComputedStyle(el);
          out.push({
            sel: sel,
            index: i,
            total: kids.length,
            scrollable: el.classList.contains('is-scrollable'),
            overflowY: cs.overflowY,
            clientHeight: el.clientHeight,
            scrollHeight: el.scrollHeight,
            maxHeight: cs.maxHeight,
            // 前 6 条是否都在可视区内（第 6 条底部 <= 容器底部 + 1px）
            sixthBottom: kids.length >= 6 ? kids[5].getBoundingClientRect().bottom : null,
            boxBottom: el.getBoundingClientRect().bottom,
          });
        });
      });
      return out;
    })()`, await ctx());

    console.log(`\n检出 ${lists.length} 个消息列表容器：`);
    lists.forEach((l) => {
      console.log(`  ${l.sel}[${l.index}] 共${l.total}条 高度=${l.clientHeight}/${l.scrollHeight} ` +
        `overflow=${l.overflowY} 可滚动=${l.scrollable} maxH=${l.maxHeight}`);
    });

    const overSix = lists.filter((l) => l.total > 6);
    if (overSix.length > 0) {
      // 条数超过 6 的区域：必须可滚动，且第 6 条刚好可见
      const notScrollable = overSix.filter((l) => l.overflowY !== 'auto' && l.overflowY !== 'scroll');
      check('超过 6 条的区域都开启了纵向滚动', notScrollable.length === 0,
        notScrollable.map((l) => `${l.sel}[${l.index}] overflow=${l.overflowY}`).join(', '));

      const overflowBad = overSix.filter((l) => l.scrollHeight <= l.clientHeight);
      check('超过 6 条的区域确有可滚动内容（scrollHeight > clientHeight）', overflowBad.length === 0,
        overflowBad.map((l) => `${l.sel}[${l.index}] ${l.scrollHeight}<=${l.clientHeight}`).join(', '));

      const sixthBad = overSix.filter((l) => l.sixthBottom !== null && l.sixthBottom > l.boxBottom + 2);
      check('第 6 条完整可见（没被裁掉）', sixthBad.length === 0,
        sixthBad.map((l) => `${l.sel}[${l.index}] 第6条底=${Math.round(l.sixthBottom)} 容器底=${Math.round(l.boxBottom)}`).join(', '));

      const seventh = overSix.filter((l) => l.scrollHeight - l.clientHeight > 2);
      check('第 7 条起的内容被隐藏到滚动区外', seventh.length === overSix.length,
        `可滚动区=${overSix.length} 有余量=${seventh.length}`);
    } else {
      console.log('（当前没有超过 6 条的区域，跳过溢出断言）');
    }

    // ---- 断言 3：滚轮能真正滚动出后续条目
    if (overSix.length > 0) {
      const target = overSix[0];
      const scrollResult = await page.evalJson(`(function () {
        var sels = ['.todo-list', '.remind-list', '.sf-list'];
        var el = null;
        for (var i = 0; i < sels.length && !el; i++) {
          el = Array.prototype.slice.call(document.querySelectorAll(sels[i]))
            .find(function (x) { return x.classList.contains('is-scrollable') && x.scrollHeight > x.clientHeight + 2; });
        }
        if (!el) return { error: '没有可滚动的容器' };
        var before = el.scrollTop;
        el.scrollTop = 9999;
        var after = el.scrollTop;
        var kids = Array.prototype.slice.call(el.children);
        var lastVisible = null;
        for (var k = 0; k < kids.length; k++) {
          var r = kids[k].getBoundingClientRect();
          var b = el.getBoundingClientRect();
          if (r.bottom > b.top && r.top < b.bottom) { lastVisible = k; }
        }
        return { before: before, after: after, scrolled: after > before, lastVisible: lastVisible, total: kids.length };
      })()`, await ctx());
      check('滚轮/滚动可查看后续条目', !!(scrollResult && scrollResult.scrolled),
        JSON.stringify(scrollResult));
      check('滚动后能看到第 6 条之后的条目',
        !!(scrollResult && scrollResult.lastVisible !== null && scrollResult.lastVisible >= 6),
        JSON.stringify(scrollResult));
    }

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
