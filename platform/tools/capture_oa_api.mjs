/**
 * 抓 OA 的真实待办接口。
 *
 * 思路：
 *   1. 起一个独立 profile 的 Chrome，开 CDP 调试端口
 *   2. **在页面加载前**注入 hook，拦住 XMLHttpRequest 和 fetch，
 *      把 url / 方法 / 参数 / 响应体记到 window.__oaReqs
 *   3. 导航到 oa.maccura.com，让用户在里面登录 + 打开「我的待办」
 *   4. 本脚本每隔几秒把捕获结果 dump 到 shots/_oa_capture.json
 *
 * 为什么要注入 hook 而不是事后读 Performance：
 *   Performance 只给 URL，拿不到响应体；而配置 OA 最需要的恰恰是
 *   返回 JSON 的结构（rows_path 和 field_map 全靠它）。
 */
import http from 'node:http';
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..', '..');
const CHROME = path.join(process.env.LOCALAPPDATA || '', 'Google/Chrome/Application/chrome.exe');
const CDP_PORT = 9385;
const PROFILE = path.join(ROOT, 'shots', '_cdp_oa');
const OUT = path.join(ROOT, 'shots', '_oa_capture.json');
const OA = process.argv[2] || 'https://oa.maccura.com';

const log = (...a) => console.log('[oa-capture]', ...a);
const sleep = ms => new Promise(r => setTimeout(r, ms));
function get(url) {
  return new Promise((res, rej) => {
    http.get(url, r => { let b = ''; r.on('data', c => (b += c)); r.on('end', () => { try { res(JSON.parse(b)); } catch (e) { rej(e); } }); })
      .on('error', rej);
  });
}

// ★ 注入到页面里的 hook。只用 ES5，兼容老版 e-cology 的页面。
const HOOK = `(function(){
  if (window.__oaHookInstalled) return;
  window.__oaHookInstalled = true;
  window.__oaReqs = [];

  function push(rec){
    try {
      // 只留可能有用的，避免把图片/css 全存进来
      var u = rec.url || '';
      if (!/\\/api\\/|\\/workflow\\/|\\/spa\\/|todo|Todo|TODO|pending|Pending|portal/i.test(u)) return;
      rec.t = Date.now();
      window.__oaReqs.push(rec);
      if (window.__oaReqs.length > 300) window.__oaReqs.shift();
    } catch(e) {}
  }

  // --- XMLHttpRequest ---
  var origOpen = XMLHttpRequest.prototype.open;
  var origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(method, url){
    this.__oa = { method: method, url: String(url), body: '' };
    return origOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function(body){
    var self = this;
    if (this.__oa) { try { this.__oa.body = typeof body === 'string' ? body.slice(0, 2000) : ''; } catch(e) {} }
    this.addEventListener('load', function(){
      try {
        if (!self.__oa) return;
        var ct = (self.getResponseHeader && self.getResponseHeader('content-type')) || '';
        var text = '';
        if (/json|text/i.test(ct) && self.responseText) text = self.responseText.slice(0, 20000);
        else if (self.responseType === 'json' && self.response) text = JSON.stringify(self.response).slice(0, 20000);
        push({ kind: 'xhr', url: self.__oa.url, method: self.__oa.method,
               reqBody: self.__oa.body, status: self.status, resp: text });
      } catch(e) {}
    });
    return origSend.apply(this, arguments);
  };

  // --- fetch ---
  if (window.fetch) {
    var origFetch = window.fetch;
    window.fetch = function(input, init){
      var url = (typeof input === 'string') ? input : (input && input.url) || '';
      var method = (init && init.method) || 'GET';
      var body = (init && typeof init.body === 'string') ? init.body.slice(0, 2000) : '';
      return origFetch.apply(this, arguments).then(function(resp){
        try {
          var clone = resp.clone();
          clone.text().then(function(text){
            push({ kind: 'fetch', url: url, method: method, reqBody: body,
                   status: resp.status, resp: (text || '').slice(0, 20000) });
          }).catch(function(){});
        } catch(e) {}
        return resp;
      });
    };
  }
})();`;

(async () => {
  if (!existsSync(CHROME)) { log('未找到 Chrome:', CHROME); process.exit(1); }
  mkdirSync(PROFILE, { recursive: true });

  const chrome = spawn(CHROME, [
    `--remote-debugging-port=${CDP_PORT}`,
    `--user-data-dir=${PROFILE}`,
    '--no-first-run', '--no-default-browser-check',
    '--disable-gpu', '--window-size=1440,1000',
    OA,
  ], { stdio: 'ignore', detached: false });

  let ws;
  try {
    let targets = null;
    for (let i = 0; i < 40; i++) {
      try { targets = await get(`http://127.0.0.1:${CDP_PORT}/json/list`); break; } catch { await sleep(500); }
    }
    const page = targets.find(t => t.type === 'page');
    if (!page) { log('没找到标签页'); process.exit(1); }

    ws = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise(r => (ws.onopen = r));
    let id = 0; const pend = new Map();
    ws.onmessage = e => { const m = JSON.parse(e.data); if (m.id && pend.has(m.id)) { pend.get(m.id)(m.result); pend.delete(m.id); } };
    const send = (method, params = {}) => new Promise(r => { const i = ++id; pend.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
    const ev = async expr => (await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true }))?.result?.value;

    await send('Page.enable');
    await send('Runtime.enable');
    // ★ 关键：新文档创建时立刻注入，才能拦住登录和待办的请求
    await send('Page.addScriptToEvaluateOnNewDocument', { source: HOOK });
    await send('Page.navigate', { url: OA });
    await sleep(2500);
    // 当前文档也补一次（addScriptToEvaluateOnNewDocument 只对新文档生效）
    await ev(HOOK);

    log('已就绪，请在打开的 Chrome 窗口里登录 OA，然后点开「我的待办」。');
    log(`抓包结果实时写入 ${OUT}`);
    log('抓到后按 Ctrl+C 结束（或让窗口一直开着，我随时能读）');

    // 持续 dump
    for (;;) {
      const raw = await ev(`JSON.stringify({url: location.href, title: document.title, count: (window.__oaReqs||[]).length, reqs: (window.__oaReqs||[])})`);
      if (raw) {
        let data;
        try { data = JSON.parse(raw); } catch { data = { error: 'parse failed' }; }
        writeFileSync(OUT, JSON.stringify(data, null, 2), 'utf8');
        const interesting = (data.reqs || []).filter(r => r.resp && /"data"|"rows"|"list"|"total"|workflow|requestId|requestName/i.test(r.resp));
        log(`页面=${(data.title || '').slice(0, 20)} | 已捕获 ${data.count} 条 | 其中有数据体的 ${interesting.length} 条`);
      }
      await sleep(4000);
    }
  } catch (err) {
    log('ERROR:', err.message);
    try { chrome.kill(); } catch {}
    process.exit(1);
  }
})();
