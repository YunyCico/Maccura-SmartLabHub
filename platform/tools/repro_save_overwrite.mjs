/**
 * 复现「保存配置后原有配置消失」的问题。
 * 用法：node platform/tools/repro_save_overwrite.mjs
 *
 * 步骤：
 *  1. 打开设置页，等配置加载完，记录 Client ID 输入框的值
 *  2. 点「保存配置」
 *  3. 再读一次 Client ID 输入框的值 + 后端返回的 config
 * 对比前后，找出是谁把值改掉了。
 */
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync } from 'node:fs';
import path from 'node:path';
import http from 'node:http';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..', '..');
const CHROME = path.join(process.env.LOCALAPPDATA || '', 'Google/Chrome/Application/chrome.exe');
const CDP_PORT = 9347;
const PROFILE = path.join(ROOT, 'shots', '_cdp_repro');
const BASE = process.argv[2] || 'http://localhost:5173';

const log = (...a) => console.log('[repro]', ...a);
function get(url) {
  return new Promise((res, rej) => {
    http.get(url, r => { let b = ''; r.on('data', c => (b += c)); r.on('end', () => res(JSON.parse(b))); })
      .on('error', rej);
  });
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  if (!existsSync(CHROME)) { log('SKIP'); return 0; }
  mkdirSync(PROFILE, { recursive: true });
  const chrome = spawn(CHROME, ['--headless=new', `--remote-debugging-port=${CDP_PORT}`,
    `--user-data-dir=${PROFILE}`, '--no-first-run', '--disable-gpu',
    '--window-size=1280,900', 'about:blank'], { stdio: 'ignore' });
  let ws;
  try {
    let t = null;
    for (let i = 0; i < 40; i++) { try { t = await get(`http://127.0.0.1:${CDP_PORT}/json/list`); break; } catch { await sleep(500); } }
    const page = t.find(x => x.type === 'page');
    ws = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise(r => (ws.onopen = r));
    let id = 0; const pend = new Map();
    ws.onmessage = e => { const m = JSON.parse(e.data); if (m.id && pend.has(m.id)) { pend.get(m.id)(m.result); pend.delete(m.id); } };
    const send = (me, p = {}) => new Promise(r => { const i = ++id; pend.set(i, r); ws.send(JSON.stringify({ id: i, method: me, params: p })); });
    const ev = async x => (await send('Runtime.evaluate', { expression: x, returnByValue: true, awaitPromise: true }))?.result?.value;

    await send('Page.enable'); await send('Runtime.enable');
    await send('Page.navigate', { url: `${BASE}/settings/integrations` });
    await sleep(4000);

    // 1. 读取所有 input 的 label + value（只关心钉钉卡片的前几个）
    const readForm = `(() => {
      const card = document.querySelector('.config-card') || document.body;
      const rows = [];
      card.querySelectorAll('label.field').forEach(lb => {
        const span = lb.querySelector('span');
        const inp = lb.querySelector('input');
        rows.push({ label: (span?.textContent||'').trim(), type: inp?.type, val: inp?.type==='password' ? (inp.value? '(有值,len='+inp.value.length+')' : '(空)') : (inp?.value||'') });
      });
      return JSON.stringify(rows);
    })()`;

    const before = JSON.parse(await ev(readForm));
    log('【保存前】表单字段：');
    before.forEach(r => log(`   ${r.label}  type=${r.type}  val=${r.val}`));

    // 2. 点「保存配置」
    const clicked = await ev(`(() => {
      const b = Array.from(document.querySelectorAll('button')).find(x => (x.textContent||'').includes('保存配置'));
      if (!b) return 'no-button';
      b.click();
      return 'clicked';
    })()`);
    log('点击保存:', clicked);
    await sleep(2500);

    // 3. 再读一次
    const after = JSON.parse(await ev(readForm));
    log('【保存后】表单字段：');
    after.forEach(r => log(`   ${r.label}  type=${r.type}  val=${r.val}`));

    // 差异
    log('');
    log('=== 差异 ===');
    let changed = 0;
    before.forEach((b, i) => {
      const a = after[i];
      if (a && b.val !== a.val) {
        changed++;
        log(`   ✗ ${b.label}: ${b.val}  →  ${a.val}`);
      }
    });
    if (!changed) log('   表单无变化');

    // 提示信息
    const msg = await ev(`(() => {
      const t = document.body.innerText || '';
      const m = t.match(/(配置已保存[^\\n]*|[^\\n]*失败[^\\n]*)/g);
      return JSON.stringify(m || []);
    })()`);
    log('页面提示:', msg);

    return 0;
  } catch (e) { log('FAIL', e.message); return 1; }
  finally { try { ws?.close(); } catch {} try { chrome.kill(); } catch {} }
})().then(c => process.exit(c));
