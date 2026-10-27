/**
 * 验证：**Vite 开发服务器(5173)** 下，两个内嵌模块都不会"套娃"。
 *
 * 为什么单独测这个：/engine 和 /portal 如果没加进 Vite 的 proxy，
 * Vite 的 SPA 兜底会把平台自己的 index.html 返回给 iframe ——
 * 页面看起来是"平台里面又套了一个平台"，报错都没有，极难定位。
 *
 * 用法：node platform/tools/verify_embedded_dev.mjs
 * 前置：平台已在跑（后端 8000 + Vite 5173）
 */
import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
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

  const env = { ...process.env, NO_PROXY: '*', no_proxy: '*' };
  for (const key of Object.keys(env)) if (/proxy/i.test(key)) delete env[key];

  let chrome = null; let profile = null;
  try {
    const debugPort = await freePort();
    profile = mkdtempSync(path.join(os.tmpdir(), 'slh_dev_'));
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

    // 登录
    await page.send('Page.navigate', { url: `${FRONTEND}/login` });
    await sleep(3000);
    await typeInto('.auth-field:nth-of-type(1) input', USERNAME);
    await typeInto('.auth-form .auth-password input', PASSWORD);
    await click('.auth-submit');
    await sleep(3000);
    const logged = await page.evalJson(`(function () {
      return { path: location.pathname, hasShell: !!document.querySelector('.brand-bar') };
    })()`, await ctx());
    check(`在开发服务器上用 ${USERNAME} 登录成功`, logged.path === '/' && logged.hasShell, JSON.stringify(logged));
    if (logged.path !== '/') {
      console.log('（登录失败，后面的检查没有意义）');
      throw new Error(`登录未成功：${logged.path}`);
    }

    // ---- 服务需求报表
    await page.send('Page.navigate', { url: `${FRONTEND}/service-demand` });
    await sleep(2500);
    let sd = null;
    for (let i = 0; i < 25; i++) {
      await sleep(800);
      sd = await page.evalJson(`(function () {
        var f = document.querySelector('iframe.report-frame');
        if (!f || !f.contentDocument) return { loaded: false };
        var d = f.contentDocument;
        return {
          loaded: true,
          src: f.getAttribute('src'),
          title: d.title,
          // 平台自己的特征：有 .app-shell 或 .brand-bar
          isPlatform: !!d.querySelector('.app-shell, .brand-bar'),
          textLen: d.body ? d.body.innerText.trim().length : 0,
        };
      })()`, await ctx()).catch(() => ({ loaded: false }));
      if (sd && sd.loaded && sd.textLen > 20) break;
    }
    check('服务需求报表 iframe 已加载', Boolean(sd?.loaded) && sd.textLen > 20,
      `title=${sd?.title} 字数=${sd?.textLen}`);
    check('服务需求报表没有套娃（不是平台页面）', sd && sd.isPlatform === false,
      `isPlatform=${sd?.isPlatform} title=${sd?.title}`);
    check('服务需求报表是真的报表页', Boolean(sd && /数据中台|DataFetch/.test(sd.title || '')),
      sd?.title || '');
    // 平台自己那层标题栏应该已经删掉了（不然顶部白白占两行）
    const ownHeader = await page.evalJson(
      `(function(){
        return {
          hasHead: !!document.querySelector('.report-head'),
          hasTitle: !!document.querySelector('.report-page > header'),
        };
      })()`,
      await ctx(),
    );
    check('平台自建标题栏已删除', !ownHeader.hasHead && !ownHeader.hasTitle, JSON.stringify(ownHeader));

    // 回工作台/重新加载 应该在报表自己的工具栏里（不是平台层）
    const embedButtons = await page.evalJson(`(function () {
      var f = document.querySelector('iframe.report-frame');
      if (!f || !f.contentDocument) return { found: false };
      var d = f.contentDocument;
      var bar = d.querySelector('.embed-actions');
      var labels = bar ? [...bar.querySelectorAll('button')].map(function (b) { return b.textContent.trim(); }) : [];
      // 确认它和「连接检测 / 重新获取」在同一行
      var sameRow = false;
      if (bar) {
        var refresh = [...d.querySelectorAll('.topbar-inner button')].find(function (b) {
          return /重新获取|获取中/.test(b.textContent);
        });
        if (refresh) {
          var a = bar.getBoundingClientRect(), c = refresh.getBoundingClientRect();
          sameRow = Math.abs(a.top + a.height / 2 - (c.top + c.height / 2)) < 12;
        }
      }
      return { found: !!bar, labels: labels, sameRow: sameRow };
    })()`, await ctx());
    check('按钮已并入报表工具栏', Boolean(embedButtons.found), (embedButtons.labels || []).join(' / '));
    check('与「连接检测/重新获取」同一行', embedButtons.sameRow === true, `sameRow=${embedButtons.sameRow}`);

    // 布局：四周不该留框（没有外层留白、没有圆角边框，iframe 顶到边）
    const layout = await page.evalJson(`(function () {
      var ws = document.querySelector('.workspace');
      var wrap = document.querySelector('.report-frame-wrap');
      var f = document.querySelector('iframe.report-frame');
      var g = function (el) { return el ? getComputedStyle(el) : null; };
      var wsCs = g(ws), wrapCs = g(wrap);
      var r = f ? f.getBoundingClientRect() : null;
      return {
        workspacePadding: wsCs ? wsCs.padding : '',
        wrapBorder: wrapCs ? wrapCs.borderTopWidth : '',
        wrapRadius: wrapCs ? wrapCs.borderTopLeftRadius : '',
        frameLeft: r ? Math.round(r.left) : -1,
        frameWidth: r ? Math.round(r.width) : -1,
        viewportWidth: window.innerWidth,
      };
    })()`, await ctx());
    check('工作区左右留白已取消', layout.workspacePadding === '0px', `padding=${layout.workspacePadding}`);
    check('报表容器没有边框', parseFloat(layout.wrapBorder) === 0, `border=${layout.wrapBorder}`);
    check('报表容器没有圆角', parseFloat(layout.wrapRadius) === 0, `radius=${layout.wrapRadius}`);
    check('报表顶到左边且铺满宽度',
      layout.frameLeft <= 1 && layout.frameWidth >= layout.viewportWidth - 2,
      `left=${layout.frameLeft} 宽=${layout.frameWidth} 视口=${layout.viewportWidth}`);

    const sdError = await page.evalJson(
      `(function(){ var b=document.querySelector('.report-banner'); return { text: b ? b.textContent.trim() : '' }; })()`,
      await ctx(),
    );
    check('页面没有报错提示', !sdError.text, sdError.text);

    // ---- BI 看板：内嵌的 iframe 不能再走绝对路径，否则整块空白
    let bi = null;
    for (let i = 0; i < 30; i++) {
      await sleep(1000);
      bi = await page.evalJson(`(function () {
        var outer = document.querySelector('iframe.report-frame');
        if (!outer || !outer.contentDocument) return { stage: 'no-outer' };
        var od = outer.contentDocument;
        var inner = od.querySelector('iframe.bi-frame');
        if (!inner) return { stage: 'no-bi-frame' };
        var doc = inner.contentDocument;
        return {
          stage: 'ok',
          src: inner.getAttribute('src'),
          hasDoc: !!doc,
          bodyLen: doc && doc.body ? doc.body.innerText.trim().length : 0,
          canvases: doc ? doc.querySelectorAll('canvas').length : 0,
        };
      })()`, await ctx()).catch(() => ({ stage: 'error' }));
      if (bi && bi.stage === 'ok') break;
    }
    check('BI 看板 iframe 出现了', Boolean(bi && bi.stage === 'ok'), `stage=${bi?.stage}`);
    check('BI 看板 src 带 /portal 前缀', /^\/portal\/bi-dashboard-template/.test(bi?.src || ''), bi?.src || '');
    check('BI 看板渲染出内容（不是空白）',
      Boolean(bi && bi.bodyLen > 100), `正文字数=${bi?.bodyLen}`);
    check('BI 看板图表已绘制', Boolean(bi && bi.canvases > 0), `canvas=${bi?.canvases}`);

    // ---- 数据分析（/engine）
    await page.send('Page.navigate', { url: `${FRONTEND}/data-analysis` });
    await sleep(2500);
    let en = null;
    for (let i = 0; i < 25; i++) {
      await sleep(800);
      en = await page.evalJson(`(function () {
        var f = document.querySelector('iframe.embed-frame');
        if (!f || !f.contentDocument) return { loaded: false };
        var d = f.contentDocument;
        return {
          loaded: true,
          src: f.getAttribute('src'),
          title: d.title,
          isPlatform: !!d.querySelector('.app-shell, .brand-bar'),
          navCount: d.querySelectorAll('.nav a[data-key]').length,
        };
      })()`, await ctx()).catch(() => ({ loaded: false }));
      if (en && en.loaded && en.navCount > 0) break;
    }
    check('数据分析工作台 iframe 已加载', Boolean(en?.loaded) && en.navCount > 0,
      `title=${en?.title} 菜单=${en?.navCount}`);
    check('数据分析工作台没有套娃', en && en.isPlatform === false, `isPlatform=${en?.isPlatform}`);

    try {
      await page.send('Page.navigate', { url: `${FRONTEND}/service-demand` });
      // 等 iframe 里真正渲染完（加载遮罩消失）再截图，否则拍到"报表加载中…"
      for (let i = 0; i < 20; i++) {
        await sleep(800);
        const ready = await page.evalJson(`(function () {
          var f = document.querySelector('iframe.report-frame');
          var mask = document.querySelector('.report-loading');
          if (!f || !f.contentDocument) return false;
          var d = f.contentDocument;
          return !mask && d.body && d.body.innerText.trim().length > 200;
        })()`, await ctx()).catch(() => false);
        if (ready) break;
      }
      await sleep(600);
      const shot = await page.send('Page.captureScreenshot', { format: 'png' });
      const shotPath = path.join(os.tmpdir(), 'slh_dev_embedded.png');
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
