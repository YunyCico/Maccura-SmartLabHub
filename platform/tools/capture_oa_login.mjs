/**
 * 抓 OA 的真实登录请求。
 *
 * 目的：搞清 VerifyLogin.jsp 到底要哪些参数、成功后响应是什么样。
 * 之前后端自己拼参数登录一直不通，只能拿浏览器真实请求对照。
 *
 * 用法：起一个 Chrome 停在登录页，用户在窗口里登录一次，
 *      脚本把那条 POST 的 URL / 请求头 / form body / 响应头 / Set-Cookie 全 dump 出来。
 */
import http from 'node:http';
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..', '..');
const CHROME = path.join(process.env.LOCALAPPDATA || '', 'Google/Chrome/Application/chrome.exe');
const CDP_PORT = 9397;
const PROFILE = path.join(ROOT, 'shots', '_cdp_oa_login');
const OUT = path.join(ROOT, 'shots', '_oa_login_capture.json');

const log = (...a) => console.log('[oa-login]', ...a);
const sleep = ms => new Promise(r => setTimeout(r, ms));
function get(url) {
  return new Promise((res, rej) => {
    http.get(url, r => { let b = ''; r.on('data', c => (b += c)); r.on('end', () => { try { res(JSON.parse(b)); } catch (e) { rej(e); } }); })
      .on('error', rej);
  });
}

if (!existsSync(CHROME)) { log('未找到 Chrome'); process.exit(1); }
mkdirSync(PROFILE, { recursive: true });

const chrome = spawn(CHROME, [
  `--remote-debugging-port=${CDP_PORT}`,
  `--user-data-dir=${PROFILE}`,
  '--no-first-run', '--no-default-browser-check',
  '--disable-gpu', '--window-size=1400,1000',
  'https://oa.maccura.com/login/Login.jsp?logintype=1',
], { stdio: 'ignore' });

let ws;
try {
  let targets = null;
  for (let i = 0; i < 40; i++) {
    try { targets = await get(`http://127.0.0.1:${CDP_PORT}/json/list`); break; } catch { await sleep(500); }
  }
  const page = targets.find(t => t.type === 'page');
  ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise(r => (ws.onopen = r));

  let id = 0; const pend = new Map();
  const reqs = new Map();
  const posts = [];
  const responses = new Map();
  const cookiesAfter = [];

  ws.onmessage = e => {
    const m = JSON.parse(e.data);
    if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); return; }
    if (m.method === 'Network.requestWillBeSent') {
      const p = m.params;
      const u = p.request.url;
      reqs.set(p.requestId, p.request);
      if (/VerifyLogin|LoginCheck|login/i.test(u) && p.request.method === 'POST') {
        posts.push({
          url: u,
          method: p.request.method,
          headers: p.request.headers,
          postData: p.request.postData || '',
          requestId: p.requestId,
        });
        log('★ 抓到登录 POST:', u.slice(0, 90));
      }
    }
    if (m.method === 'Network.responseReceived') {
      const p = m.params;
      const r = reqs.get(p.requestId);
      if (r && /VerifyLogin|LoginCheck/i.test(r.url)) {
        responses.set(p.requestId, {
          status: p.response.status,
          headers: p.response.headers,
          url: p.response.url,
        });
        // Set-Cookie 也可能在 headers 里
        const sc = p.response.headers['set-cookie'] || p.response.headers['Set-Cookie'];
        if (sc) log('  Set-Cookie:', String(sc).slice(0, 200));
      }
    }
    if (m.method === 'Network.responseReceivedExtraInfo') {
      const h = m.params.headers || {};
      if (h['set-cookie'] || h['Set-Cookie']) {
        cookiesAfter.push({ requestId: m.params.requestId, setCookie: h['set-cookie'] || h['Set-Cookie'] });
      }
    }
  };

  const send = (method, params = {}) => new Promise(r => {
    const i = ++id; pend.set(i, r); ws.send(JSON.stringify({ id: i, method, params }));
  });

  await send('Page.enable');
  await send('Network.enable');

  log('');
  log('  请在打开的 Chrome 窗口里登录一次 OA（账号密码你自己输，我不需要知道）。');
  log('  登录成功后停在这个窗口，我会自动抓到请求。');
  log('');

  // 轮询：抓到 POST 且页面已经离开 Login.jsp 就收工
  const deadline = Date.now() + 8 * 60 * 1000;
  let done = false;
  while (Date.now() < deadline && !done) {
    await sleep(3000);
    for (const p of posts) {
      const resp = responses.get(p.requestId);
      if (resp) {
        done = true;
        const report = { url: p.url, method: p.method, headers: p.headers, postData: p.postData, response: resp, setCookies: cookiesAfter };
        writeFileSync(OUT, JSON.stringify(report, null, 2), 'utf8');
        log('');
        log('========== 登录请求（真实） ==========');
        log('URL:', p.url);
        log('方法:', p.method);
        log('响应状态:', resp.status);
        log('');
        log('--- form body（这就是后端要照抄的）---');
        try {
          const params = new URLSearchParams(p.postData);
          for (const [k, v] of params) log(`  ${k} = ${String(v).slice(0, 60)}`);
        } catch {
          log('  ' + p.postData.slice(0, 500));
        }
        log('');
        log('--- 关键请求头 ---');
        for (const [k, v] of Object.entries(p.headers || {})) {
          if (/^(content-type|referer|origin|user-agent|accept)$/i.test(k)) log(`  ${k}: ${String(v).slice(0, 110)}`);
        }
        log('');
        log('--- 响应头里的 Set-Cookie ---');
        const sc = resp.headers['set-cookie'] || resp.headers['Set-Cookie'];
        log('  ' + (sc ? String(sc).slice(0, 400) : '(无)'));
        log('');
        log('已保存:', OUT);
      }
    }
  }
  if (!done) log('超时：没抓到登录 POST。');
  try { chrome.kill(); } catch {}
  process.exit(0);
} catch (err) {
  log('ERROR:', err.message);
  try { chrome.kill(); } catch {}
  process.exit(1);
}
