/**
 * 验证：Notifications Center 自定义区块排序（2026-10-27）。
 *
 * 覆盖三件事：
 *   1. 三个来源（钉钉 / Salesforce / OA）能上下移动，视觉顺序真的变了
 *   2. 每个来源内部的小卡片能上下移动（钉钉 kind、Salesforce 卡片、OA 卡片）
 *   3. 顺序存到账号上 —— 刷新页面后还是用户排的那套
 *
 * 用真实 Chrome + 真实登录，点的是真实的 ↑↓ 按钮（拖拽在 headless 里
 * 靠 Input.dispatchMouseEvent 模拟不稳定，用同一条排序链路的按钮验证更可靠；
 * 拖拽和按钮共用 moveTo/nudge → scheduleSaveNcOrder，逻辑等价）。
 *
 * 用法：node platform/tools/verify_nc_sort.mjs
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
    profile = mkdtempSync(path.join(os.tmpdir(), 'slh_ncsort_'));
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
      await sleep(900);
    }

    // ---- 登录
    await page.send('Page.navigate', { url: `${FRONTEND}/login` });
    await sleep(3000);
    await typeInto('.auth-field:nth-of-type(1) input', USERNAME);
    await typeInto('.auth-form .auth-password input', PASSWORD);
    await click('.auth-submit');
    await sleep(4000);
    const logged = await page.evalJson(`(function () { return { path: location.pathname }; })()`, await ctx());
    check(`用 ${USERNAME} 登录成功`, logged.path === '/', JSON.stringify(logged));
    if (logged.path !== '/') throw new Error('登录未成功');

    await sleep(3500);

    /** 读当前视觉顺序（按 getBoundingClientRect().top 排，这才是用户看到的）。 */
    const READ_ORDER = `(function () {
      function visual(sel, attr) {
        return Array.prototype.slice.call(document.querySelectorAll(sel))
          .map(function (el) {
            var r = el.getBoundingClientRect();
            return { id: el.getAttribute(attr) || '', key: attr, top: Math.round(r.top + window.scrollY),
                     label: (el.querySelector('h4, h5') || {}).textContent || '' };
          })
          .sort(function (a, b) { return a.top - b.top; });
      }
      return {
        sources: Array.prototype.slice.call(document.querySelectorAll('.nc-source'))
          .map(function (el) {
            var r = el.getBoundingClientRect();
            var name = (el.querySelector('.nc-source-name') || {}).textContent || '';
            return { name: name.trim(), top: Math.round(r.top + window.scrollY), styleOrder: el.style.order };
          })
          .sort(function (a, b) { return a.top - b.top; })
          .map(function (x) { return x.name; }),
        kinds: Array.prototype.slice.call(document.querySelectorAll('.nc-kind'))
          .map(function (el) {
            var r = el.getBoundingClientRect();
            var name = (el.querySelector('h5') || {}).textContent || '';
            return { name: name.trim(), top: Math.round(r.top + window.scrollY) };
          })
          .sort(function (a, b) { return a.top - b.top; })
          .map(function (x) { return x.name; }),
        // ⚠️ 卡片区是两列 grid，同一行两张卡 top 相同 —— 只按 top 排序看不出左右互换。
        //    必须用 (top, left) 复合键，才是真正的视觉阅读顺序。
        sfCards: Array.prototype.slice.call(document.querySelectorAll('.nc-source-salesforce .sf-card'))
          .map(function (el) {
            var r = el.getBoundingClientRect();
            var name = (el.querySelector('h4') || {}).textContent || '';
            return { name: name.trim(), key: Math.round(r.top + window.scrollY) * 10000 + Math.round(r.left) };
          })
          .sort(function (a, b) { return a.key - b.key; })
          .map(function (x) { return x.name; }),
        oaCards: Array.prototype.slice.call(document.querySelectorAll('.nc-source-oa .sf-card'))
          .map(function (el) {
            var r = el.getBoundingClientRect();
            var name = (el.querySelector('h4') || {}).textContent || '';
            return { name: name.trim(), key: Math.round(r.top + window.scrollY) * 10000 + Math.round(r.left) };
          })
          .sort(function (a, b) { return a.key - b.key; })
          .map(function (x) { return x.name; }),
      };
    })()`;

    // 先备份账号原有偏好，验证完恢复 —— 不能让自动化改动真人账号的排序
    const originalPrefs = await page.evalJson(`(function () {
      return fetch('/api/auth/prefs', { credentials: 'same-origin' })
        .then(function (r) { return r.json(); })
        .then(function (j) { return j.prefs || {}; });
    })()`, await ctx());

    // ---- 基线
    const before = await page.evalJson(READ_ORDER, await ctx());
    console.log('初始来源顺序：', JSON.stringify(before.sources));
    console.log('初始钉钉卡片顺序：', JSON.stringify(before.kinds));

    check('来源数量为 3', before.sources.length === 3, JSON.stringify(before.sources));
    check('可拖动的东西存在（.is-sortable）',
      await page.evalJson(`(function () { return document.querySelectorAll('.is-sortable[draggable="true"]').length; })()`, await ctx()) >= 4,
      '来源+卡片都挂了 draggable');

    // ---- 来源排序：把第一个来源往下挪一位，看视觉顺序是否交换
    const firstSourceName = before.sources[0];
    const movedSource = await page.evalJson(`(function () {
      var sections = Array.prototype.slice.call(document.querySelectorAll('.nc-source'));
      // 找出视觉上最靠上的那个来源，点它 header 里的「↓」
      var top = sections.slice().sort(function (a, b) {
        return a.getBoundingClientRect().top - b.getBoundingClientRect().top;
      })[0];
      if (!top) return { error: '没有来源区块' };
      var btn = top.querySelector('.nc-source-head .nc-sort-nudge button:last-child');
      if (!btn) return { error: '没找到下移按钮' };
      if (btn.disabled) return { error: '按钮是禁用的（已经是最后一个）' };
      var name = (top.querySelector('.nc-source-name') || {}).textContent || '';
      btn.click();
      return { clicked: true, name: name.trim() };
    })()`, await ctx());
    await sleep(1800);
    const afterMove = await page.evalJson(READ_ORDER, await ctx());
    console.log('下移后来源顺序：', JSON.stringify(afterMove.sources));

    check('点击「↓」后来源顺序发生变化',
      JSON.stringify(afterMove.sources) !== JSON.stringify(before.sources),
      `前=${JSON.stringify(before.sources)} 后=${JSON.stringify(afterMove.sources)}`);
    check('被移动的来源确实换到了第二位',
      afterMove.sources[1] === firstSourceName,
      `第一位移前的来源=${firstSourceName} 现在第二位=${afterMove.sources[1]}`);

    // ---- 内部卡片排序：钉钉第一个卡片下移
    const beforeKinds = afterMove.kinds;
    const movedKind = await page.evalJson(`(function () {
      var cards = Array.prototype.slice.call(document.querySelectorAll('.nc-kind'));
      if (cards.length < 2) return { error: '卡片不足 2 个', count: cards.length };
      var top = cards.slice().sort(function (a, b) {
        var ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
        var ka = Math.round(ra.top) * 10000 + Math.round(ra.left);
        var kb = Math.round(rb.top) * 10000 + Math.round(rb.left);
        return ka - kb;
      })[0];
      var btn = top.querySelector('.nc-kind-head .nc-sort-nudge button:last-child');
      if (!btn) return { error: '没找到下移按钮' };
      var name = (top.querySelector('h5') || {}).textContent || '';
      btn.click();
      return { clicked: true, name: name.trim(), count: cards.length };
    })()`, await ctx());
    await sleep(1800);
    const afterKind = await page.evalJson(READ_ORDER, await ctx());

    if (movedKind && movedKind.clicked) {
      check('点击「↓」后钉钉卡片顺序发生变化',
        JSON.stringify(afterKind.kinds) !== JSON.stringify(beforeKinds),
        `前=${JSON.stringify(beforeKinds)} 后=${JSON.stringify(afterKind.kinds)}`);
    } else {
      console.log('（钉钉卡片不足 2 个，跳过内部排序断言）', JSON.stringify(movedKind));
    }

    // ---- Salesforce 卡片排序（.sf-card 用的是 CSS order，与钉钉 kind 的数组重排不同）
    const sfBefore = afterKind.sfCards;
    const sfClick = await page.evalJson(`(function () {
      var cards = Array.prototype.slice.call(document.querySelectorAll('.nc-source-salesforce .sf-card'));
      if (cards.length < 2) return { error: '卡片不足', count: cards.length };
      var top = cards.slice().sort(function (a, b) {
        var ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
        var ka = Math.round(ra.top) * 10000 + Math.round(ra.left);
        var kb = Math.round(rb.top) * 10000 + Math.round(rb.left);
        return ka - kb;
      })[0];
      var btn = top.querySelector('.sf-card-head .nc-sort-nudge button:last-child');
      if (!btn) return { error: '没找到下移按钮' };
      var name = (top.querySelector('h4') || {}).textContent || '';
      btn.click();
      return { clicked: true, name: name.trim() };
    })()`, await ctx());
    await sleep(1800);
    const sfAfter = await page.evalJson(READ_ORDER, await ctx());
    if (sfClick && sfClick.clicked) {
      check('点击「↓」后 Salesforce 卡片顺序发生变化',
        JSON.stringify(sfAfter.sfCards) !== JSON.stringify(sfBefore),
        `前=${JSON.stringify(sfBefore)} 后=${JSON.stringify(sfAfter.sfCards)}`);
      check('Salesforce 被移动的卡片落到第二位',
        sfAfter.sfCards[1] === sfBefore[0],
        `原第一位=${sfBefore[0]} 现在第二位=${sfAfter.sfCards[1]}`);
    } else {
      console.log('（Salesforce 卡片不足 2 张，跳过）', JSON.stringify(sfClick));
    }

    // ---- OA 卡片排序
    const oaBefore = sfAfter.oaCards;
    const oaClick = await page.evalJson(`(function () {
      var cards = Array.prototype.slice.call(document.querySelectorAll('.nc-source-oa .sf-card'));
      if (cards.length < 2) return { error: '卡片不足', count: cards.length };
      var top = cards.slice().sort(function (a, b) {
        var ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
        var ka = Math.round(ra.top) * 10000 + Math.round(ra.left);
        var kb = Math.round(rb.top) * 10000 + Math.round(rb.left);
        return ka - kb;
      })[0];
      var btn = top.querySelector('.sf-card-head .nc-sort-nudge button:last-child');
      if (!btn) return { error: '没找到下移按钮' };
      var name = (top.querySelector('h4') || {}).textContent || '';
      btn.click();
      return { clicked: true, name: name.trim() };
    })()`, await ctx());
    await sleep(1800);
    const oaAfter = await page.evalJson(READ_ORDER, await ctx());
    if (oaClick && oaClick.clicked) {
      check('点击「↓」后 OA 卡片顺序发生变化',
        JSON.stringify(oaAfter.oaCards) !== JSON.stringify(oaBefore),
        `前=${JSON.stringify(oaBefore)} 后=${JSON.stringify(oaAfter.oaCards)}`);
    } else {
      console.log('（OA 卡片不足 2 张，跳过）', JSON.stringify(oaClick));
    }

    // ---- 持久化：等 debounce 落库
    await sleep(2000);
    const savedPrefs = await page.evalJson(`(function () {
      return fetch('/api/auth/prefs', { credentials: 'same-origin' })
        .then(function (r) { return r.json(); })
        .then(function (j) { return j.prefs || {}; })
        .catch(function (e) { return { error: String(e) }; });
    })()`, await ctx());
    console.log('已保存的偏好：', JSON.stringify(savedPrefs));
    check('排序已写入账号偏好（有 nc.sources）',
      !!(savedPrefs && savedPrefs.nc && Array.isArray(savedPrefs.nc.sources)),
      JSON.stringify(savedPrefs && savedPrefs.nc && savedPrefs.nc.sources));
    check('保存的来源顺序 = 当前视觉顺序',
      !!(savedPrefs && savedPrefs.nc && savedPrefs.nc.sources &&
         savedPrefs.nc.sources.join(',') === (await page.evalJson(`(function () {
           // 内部顺序：按 style.order 取（等价于视觉顺序，但不受滚动影响）
           var list = Array.prototype.slice.call(document.querySelectorAll('.nc-source'));
           // 从 prefs 里对应不上的话就用视觉顺序
           return Array.prototype.slice.call(document.querySelectorAll('.nc-source'))
             .map(function (el) {
               var r = el.getBoundingClientRect();
               var cls = el.className;
               var key = cls.indexOf('dingtalk') >= 0 ? 'dingtalk'
                       : cls.indexOf('salesforce') >= 0 ? 'salesforce' : 'oa';
               return { key: key, top: r.top + window.scrollY };
             })
             .sort(function (a, b) { return a.top - b.top; })
             .map(function (x) { return x.key; });
         })()`, await ctx())).join(',')),
      JSON.stringify(savedPrefs && savedPrefs.nc && savedPrefs.nc.sources));

    // ---- 刷新后保持
    await page.send('Page.reload', { ignoreCache: false });
    await sleep(7000);
    const afterReload = await page.evalJson(READ_ORDER, await ctx());
    console.log('刷新后来源顺序：', JSON.stringify(afterReload.sources));
    check('刷新页面后来源顺序保持不变',
      JSON.stringify(afterReload.sources) === JSON.stringify(afterMove.sources),
      `刷新前=${JSON.stringify(afterMove.sources)} 刷新后=${JSON.stringify(afterReload.sources)}`);

    // ---- 收尾：把账号偏好恢复成跑之前的样子
    // 这是真人账号，验证不能留下自己排的顺序。
    const restored = await page.evalJson(`(function (payload) {
      return fetch('/api/auth/prefs', {
        method: 'PATCH',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
        .then(function (r) { return r.json(); })
        .then(function (j) { return { ok: j.ok, prefs: j.prefs }; })
        .catch(function (e) { return { error: String(e) }; });
    })(${JSON.stringify(originalPrefs || {})})`, await ctx());
    check('验证结束后已恢复账号原有排序偏好',
      !!(restored && restored.ok && JSON.stringify(restored.prefs) === JSON.stringify(originalPrefs || {})),
      `恢复为=${JSON.stringify(restored && restored.prefs)}`);

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
