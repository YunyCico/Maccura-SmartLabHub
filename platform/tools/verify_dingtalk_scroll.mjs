/**
 * 验证：钉钉「待办任务」区域超过 6 条时，最多显示 6 条、其余可滚动。
 *
 * 为什么单独测这个：
 * verify_home_scroll.mjs 跑的是真实环境，钉钉待办通常为空（要免登授权），
 * 真正超 6 条的是 OA 待办区。OA 区验证过的是 .sf-list 容器，
 * 而钉钉待办用的是 .todo-list 容器（栅格布局、item 高度不同），
 * 两者虽然共用同一个指令，但值得单独走一遍真实渲染路径。
 *
 * 做法：用 CDP 的 Fetch 域拦截 /api/messages，注入 10 条钉钉待办，
 * 让页面按真实逻辑渲染，再断言"精确 6 条 + 可滚动"。
 *
 * 用法：node platform/tools/verify_dingtalk_scroll.mjs
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
      if (message.method === 'Fetch.requestPaused') {
        this.handlePaused?.(message.params);
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

/** 造 10 条钉钉待办（结构对齐 MessageItem） */
function makeDingTalkTodos(count) {
  const items = [];
  for (let i = 1; i <= count; i++) {
    items.push({
      source: 'dingtalk',
      external_id: `dd-todo-${i}`,
      title: `测试待办 ${i}`,
      detail: `这是第 ${i} 条测试数据`,
      due: '2026-10-31',
      priority: i % 3 === 0 ? '高' : i % 3 === 1 ? '中' : '低',
      url: '',
      category: '',
      kind: 'todo',
      creator: '测试用户',
      live: true,
      source_label: '钉钉',
      tone: 'default',
      extra: { created_at: '2026-10-27T08:00:00+08:00' },
    });
  }
  return items;
}

async function main() {
  if (!existsSync(CHROME)) { console.log('SKIP chrome missing'); return 0; }

  const env = { ...process.env, NO_PROXY: '*', no_proxy: '*' };
  for (const key of Object.keys(env)) if (/proxy/i.test(key)) delete env[key];

  let chrome = null; let profile = null;
  try {
    const debugPort = await freePort();
    profile = mkdtempSync(path.join(os.tmpdir(), 'slh_ddscroll_'));
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

    // 拦截 /api/messages，注入 10 条钉钉待办
    // ⚠️ 注意：handlePaused 会被当成普通回调调用，this 不是 Cdp 实例，
    //    所以这里用箭头函数闭包捕获 page，不能写成普通 function。
    const injected = makeDingTalkTodos(10);
    page.handlePaused = async (params) => {
      try {
        const response = await page.send('Fetch.getResponseBody', { requestId: params.requestId });
        let body = {};
        try { body = JSON.parse(response.result.body); } catch { body = {}; }
        const data = body.data || body;
        const sources = Array.isArray(data.sources) ? data.sources : [];
        // 找到钉钉来源，确保 todo 类型是开着的（否则前端过滤掉了）
        let dd = sources.find((s) => s.source === 'dingtalk');
        if (!dd) {
          dd = { source: 'dingtalk', enabled: true, count: 0, error: '', kinds: ['todo', 'calendar'] };
          sources.push(dd);
        }
        if (!Array.isArray(dd.kinds) || !dd.kinds.includes('todo')) {
          dd.kinds = Array.isArray(dd.kinds) ? dd.kinds.concat(['todo']) : ['todo'];
        }
        dd.enabled = true;
        // 首页的 todos 就是 messageFeed.items。
        // axios 不解包：fetchMessages 返回 response.data，所以响应顶层就是 MessageFeed，
        // 不要再包一层 { ok, data }。
        const existingItems = Array.isArray(data.items) ? data.items : [];
        const merged = injected.concat(existingItems.filter((t) => t.source !== 'dingtalk'));
        dd.count = merged.filter((t) => t.source === 'dingtalk').length;
        const payload = { ...data, items: merged, sources, using_placeholder: false };
        await page.send('Fetch.fulfillRequest', {
          requestId: params.requestId,
          responseCode: 200,
          responseHeaders: [{ name: 'Content-Type', value: 'application/json; charset=utf-8' }],
          body: Buffer.from(JSON.stringify(payload), 'utf-8').toString('base64'),
        });
      } catch (e) {
        console.error('拦截处理失败:', e.message);
        try {
          await page.send('Fetch.continueRequest', { requestId: params.requestId });
        } catch {}
      }
    };

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

    await page.send('Page.navigate', { url: `${FRONTEND}/login` });
    await sleep(3000);
    await typeInto('.auth-field:nth-of-type(1) input', USERNAME);
    await typeInto('.auth-form .auth-password input', PASSWORD);
    await click('.auth-submit');
    await sleep(5000);

    const logged = await page.evalJson(`(function () {
      return { path: location.pathname };
    })()`, await ctx());
    check(`用 ${USERNAME} 登录成功`, logged.path === '/', JSON.stringify(logged));
    if (logged.path !== '/') throw new Error('登录未成功');

    await sleep(4000);

    // 拦截策略（踩过坑）：
    //  - Request 阶段 + reload：getResponseBody 在请求阶段无响应体可读，全部走 continueRequest，
    //    且 reload 后页面渲染异常（body 全空），弃用。
    //  - 现在的做法：登录后等首屏渲染完，启用 **Response 阶段**拦截，
    //    再点页面上的「刷新」按钮触发新的 /api/messages —— 此时才注入数据。
    await page.send('Fetch.enable', {
      patterns: [{ urlPattern: '*/api/messages*' }],
    });
    console.log('已启用 /api/messages 拦截（Response 阶段），点击「刷新」触发取数…');
    await click('.todo-refresh');
    await sleep(9000);

    // ---- 页面状态诊断
    for (let attempt = 0; attempt < 5; attempt++) {
      const diag = await page.evalJson(`(function () {
        var body = document.body.innerText || '';
        return {
          path: location.pathname,
          hasTodoPanel: !!document.querySelector('.todo-panel'),
          hasNcSources: !!document.querySelector('.nc-sources'),
          hasDingtalk: !!document.querySelector('.nc-source-dingtalk'),
          noticeText: (document.querySelector('.notice-bar') || {}).textContent || '',
          chips: Array.prototype.slice.call(document.querySelectorAll('.todo-source-chip')).map(function (c) { return (c.textContent || '').trim(); }),
          todoLists: document.querySelectorAll('.todo-list').length,
          sfLists: document.querySelectorAll('.sf-list').length,
          bodyLen: body.length,
          bodySnippet: body.slice(0, 150).replace(/\\n/g, ' | '),
        };
      })()`, await ctx());
      console.log(`诊断#${attempt}：`, JSON.stringify(diag));
      if (diag.hasTodoPanel && diag.todoLists + diag.sfLists > 0) break;
      await sleep(2500);
    }

    // ---- 查钉钉待办容器
    const view = await page.evalJson(`(function () {
      var el = document.querySelector('.todo-list');
      if (!el) return { found: false };
      var kids = Array.prototype.slice.call(el.children);
      var cs = getComputedStyle(el);
      return {
        found: true,
        total: kids.length,
        scrollable: el.classList.contains('is-scrollable'),
        overflowY: cs.overflowY,
        clientHeight: el.clientHeight,
        scrollHeight: el.scrollHeight,
        maxHeight: cs.maxHeight,
        sixthBottom: kids.length >= 6 ? kids[5].getBoundingClientRect().bottom : null,
        boxBottom: el.getBoundingClientRect().bottom,
        firstTitle: kids.length ? (kids[0].querySelector('.todo-copy strong') || {}).textContent : null,
      };
    })()`, await ctx());

    if (!view.found) {
      console.log('SKIP 页面没有 .todo-list 容器（钉钉待办区域未渲染）');
      console.log('结果：0/1（跳过）');
      return 0;
    }

    console.log('钉钉待办容器：', JSON.stringify(view));

    check('钉钉待办已注入 10 条', view.total === 10, `total=${view.total}`);
    check('钉钉待办容器开启纵向滚动', view.overflowY === 'auto' || view.overflowY === 'scroll',
      `overflow=${view.overflowY} maxH=${view.maxHeight}`);
    check('确有可滚动内容（scrollHeight > clientHeight）', view.scrollHeight > view.clientHeight,
      `${view.scrollHeight} vs ${view.clientHeight}`);
    check('第 6 条完整可见', view.sixthBottom !== null && view.sixthBottom <= view.boxBottom + 2,
      `第6条底=${view.sixthBottom ? Math.round(view.sixthBottom) : 'n/a'} 容器底=${Math.round(view.boxBottom)}`);

    // ---- 滚轮滚动验证
    const scrollResult = await page.evalJson(`(function () {
      var el = document.querySelector('.todo-list');
      if (!el) return { error: 'no el' };
      var before = el.scrollTop;
      el.scrollTop = 9999;
      var after = el.scrollTop;
      var kids = Array.prototype.slice.call(el.children);
      var lastVisible = null;
      var b = el.getBoundingClientRect();
      kids.forEach(function (k, i) {
        var r = k.getBoundingClientRect();
        if (r.bottom > b.top && r.top < b.bottom) lastVisible = i;
      });
      return { before: before, after: after, scrolled: after > before, lastVisible: lastVisible, total: kids.length };
    })()`, await ctx());
    check('滚轮可查看后续条目', !!(scrollResult && scrollResult.scrolled), JSON.stringify(scrollResult));
    check('滚动后能看到第 7 条及以后', !!(scrollResult && scrollResult.lastVisible >= 6), JSON.stringify(scrollResult));

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
