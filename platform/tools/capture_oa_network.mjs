/**
 * 在已登录的 OA 浏览器里导航到待办页，用 CDP 的 Network 事件抓真实请求。
 *
 * 为什么用 Network 事件而不是页面内 hook：
 *   泛微 main.jsp 是 frameset，待办内容在多层 iframe 里，
 *   页面内 hook 只能拿到单个 frame 的记录；Network 是浏览器层面的，全覆盖。
 */
import http from 'node:http';

function get(url) {
  return new Promise((res, rej) => {
    http.get(url, r => { let b = ''; r.on('data', c => (b += c)); r.on('end', () => { try { res(JSON.parse(b)); } catch (e) { rej(e); } }); })
      .on('error', rej);
  });
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

const target = process.argv[2] || 'https://oa.maccura.com/workflow/request/RequestView.jsp';
const targets = await get('http://127.0.0.1:9385/json/list');
const page = targets.find(t => t.type === 'page');
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise(r => (ws.onopen = r));

let id = 0; const pend = new Map();
const reqs = new Map();   // requestId -> {url, method, type}
const finished = [];      // 已完成的
ws.onmessage = e => {
  const m = JSON.parse(e.data);
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); return; }
  if (m.method === 'Network.requestWillBeSent') {
    const p = m.params;
    reqs.set(p.requestId, {
      url: p.request.url,
      method: p.request.method,
      type: p.type,
      postData: (p.request.postData || '').slice(0, 500),
    });
  }
  if (m.method === 'Network.responseReceived') {
    const p = m.params;
    const r = reqs.get(p.requestId);
    if (r) { r.status = p.response.status; r.mime = p.response.mimeType; r.finishedId = p.requestId; }
  }
};
const send = (method, params = {}) => new Promise(r => { const i = ++id; pend.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });

await send('Page.enable');
await send('Network.enable');
await send('Page.navigate', { url: target });
await sleep(9000);

// 只关心可能带数据的：XHR / Fetch / 含 workflow 或 todo 的
const interesting = [...reqs.values()].filter(r => {
  const u = r.url || '';
  if (/\.(js|css|png|jpg|jpeg|gif|svg|woff2?|ico)(\?|$)/i.test(u)) return false;
  return r.type === 'XHR' || r.type === 'Fetch' || /workflow|todo|request|list|portal/i.test(u);
});

console.log(`共 ${reqs.size} 个请求，其中可疑的 ${interesting.length} 个：\n`);
for (const r of interesting) {
  console.log(`[${r.status || '-'}] ${r.method} ${String(r.url).slice(0, 130)}`);
  console.log(`      type=${r.type} mime=${r.mime || '-'}`);
  if (r.postData) console.log(`      post=${r.postData.replace(/\s+/g, ' ')}`);
  // 取响应体（只对 XHR/Fetch 且是 json/html 的）
  if (r.finishedId && (r.type === 'XHR' || r.type === 'Fetch')) {
    try {
      const body = await send('Network.getResponseBody', { requestId: r.finishedId });
      const text = body?.result?.body || '';
      if (text) {
        console.log(`      resp(${text.length}): ${text.slice(0, 400).replace(/\s+/g, ' ')}`);
      }
    } catch { /* 有些响应拿不到 body */ }
  }
  console.log('');
}
ws.close();
