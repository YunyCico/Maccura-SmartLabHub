/**
 * 真实交互验证：LIS「标准字段设置」。
 *   ① 能打开设置弹窗，表格只显示「列名/必填/类型/说明/来源」（英文字段名、分组两列已取消）
 *   ② 能改中文名、设必填、新增字段
 *   ③ 删除是「先标记、保存后生效」：点一次画横线，再点一次撤销
 *   ④ 跑完把配置还原成运行前的样子（绝不恢复出厂，避免动到用户真实设置）
 *
 * 用法：node platform/tools/verify_lis_fields_ui.mjs
 * 前置：平台已启动（5173 = Vue 外壳，8000 = 后端，8765 = 原生引擎）。
 */
import { spawn } from 'node:child_process';
import { existsSync, writeFileSync } from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..', '..');
const CHROME = path.join(process.env.LOCALAPPDATA || '', 'Google/Chrome/Application/chrome.exe');
const APP = 'http://127.0.0.1:5173/data-analysis';
const OUT = path.join(ROOT, 'platform', 'tools', '_lis_fields_ui.png');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.listen(0, '127.0.0.1', () => {
      const p = server.address().port;
      server.close(() => resolve(p));
    });
    server.on('error', reject);
  });
}

class Cdp {
  constructor(ws) {
    this.ws = ws; this.id = 0; this.pending = new Map(); this.errors = [];
    this.sessions = new Map();   // targetId → sessionId（跨源 iframe = OOPIF，独立 target）
    this.contexts = new Map();   // frameId → contextId
    ws.addEventListener('message', (ev) => {
      const m = JSON.parse(ev.data);
      if (m.id && this.pending.has(m.id)) {
        const p = this.pending.get(m.id); this.pending.delete(m.id); p.resolve(m); return;
      }
      if (m.method === 'Target.attachedToTarget') {
        const info = m.params || {};
        this.sessions.set(info.targetInfo && info.targetInfo.targetId, info.sessionId);
      }
      if (m.method === 'Runtime.executionContextCreated') {
        const ctx = m.params.context;
        const frameId = m.params.auxData && m.params.auxData.frameId;
        if (frameId) this.contexts.set(frameId, ctx.id);
      }
      if (m.method === 'Runtime.exceptionThrown') {
        const d = m.params?.exceptionDetails || {};
        this.errors.push(d.exception?.description || d.text || 'runtime exception');
      }
    });
  }
  send(method, params = {}, timeoutMs = 90000, sessionId) {
    const id = ++this.id;
    const payload = { id, method, params };
    if (sessionId) payload.sessionId = sessionId;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error('timeout ' + method)); }, timeoutMs);
      this.pending.set(id, { resolve: (m) => { clearTimeout(timer); resolve(m); } });
      this.ws.send(JSON.stringify(payload));
    });
  }
  async evaluate(expression, sessionId, contextId) {
    const params = { expression, returnByValue: true, awaitPromise: true };
    if (contextId) params.contextId = contextId;
    const r = await this.send('Runtime.evaluate', params, 90000, sessionId);
    if (r?.result?.exceptionDetails) {
      throw new Error('页面异常: ' + (r.result.exceptionDetails.exception?.description || r.result.exceptionDetails.text));
    }
    const v = r?.result?.result?.value;
    if (v === undefined) throw new Error('evaluate 无返回值: ' + JSON.stringify(r).slice(0, 300));
    return v;
  }
  /** 找到跨源 iframe（OOPIF）的 sessionId —— 在它里面执行才有"从 iframe 发出"的语义 */
  async iframeSession() {
    const list = await (await fetch(`http://127.0.0.1:${this.port}/json/list`)).json();
    const frame = list.find((t) => t.type === 'iframe' || (t.url && t.url.includes('8765')));
    if (!frame) return '';
    for (let i = 0; i < 40 && !this.sessions.get(frame.id); i++) await sleep(250);
    return this.sessions.get(frame.id) || '';
  }

  /** 在 iframe 那个 frame 里拿到可执行的 contextId */
  async iframeContextId() {
    const tree = await this.send('Page.getFrameTree');
    const child = tree?.result?.frameTree?.childFrames?.[0];
    if (!child) return 0;
    const frameId = child.frame.id;
    for (let i = 0; i < 20; i++) {
      if (this.contexts.has(frameId)) return this.contexts.get(frameId);
      await sleep(250);
    }
    // 退路：给这个 frame 建一个隔离世界（universal access 能拿到 window.parent）
    const made = await this.send('Page.createIsolatedWorld', {
      frameId, worldName: 'lis-probe', grantUniveralAccess: true,
    });
    return made?.result?.executionContextId || 0;
  }
}

function openWs(url) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(url);
    ws.addEventListener('open', () => resolve(ws), { once: true });
    ws.addEventListener('error', () => reject(new Error('ws error')), { once: true });
  });
}

async function waitHttp(url, timeoutMs) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    try { const r = await fetch(url, { signal: AbortSignal.timeout(2500) }); if (r.ok) return true; } catch {}
    await sleep(400);
  }
  return false;
}

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok, detail });
  console.log(`  ${ok ? '✅' : '❌'} ${name}${detail ? ' — ' + detail : ''}`);
}

async function main() {
  if (!existsSync(CHROME)) { console.error('Chrome 不存在'); process.exit(2); }
  if (!await waitHttp('http://127.0.0.1:5173/', 15000)) { console.error('Vue 外壳未启动（5173）'); process.exit(2); }

  const port = await freePort();
  const profile = path.join(os.tmpdir(), 'slh_fs_profile_' + Date.now());
  const chrome = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--hide-scrollbars', '--no-first-run',
    '--remote-debugging-port=' + port, '--user-data-dir=' + profile,
    '--window-size=1500,1150', 'about:blank',
  ], { stdio: 'ignore' });

  let ws;
  try {
    let target = null;
    for (let i = 0; i < 60 && !target; i++) {
      await sleep(400);
      try {
        const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
        target = list.find((t) => t.type === 'page');
      } catch {}
    }
    if (!target) throw new Error('没找到 Chrome page target');
    ws = await openWs(target.webSocketDebuggerUrl);
    const cdp = new Cdp(ws);
    cdp.port = port;
    await cdp.send('Page.enable');
    await cdp.send('Runtime.enable');
    // 跨源 iframe = OOPIF，要自动 attach 才能拿到它的 session
    await cdp.send('Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: false, flatten: true });

    await cdp.send('Page.navigate', { url: APP });
    await sleep(5000);

    // ---- 唤起 LIS 数据整理（消息必须从 iframe 内部发出：父窗口 postMessage 过不了 event.source 校验）----
    const ctx = await cdp.iframeContextId();
    check('拿到 iframe 执行上下文', !!ctx, 'contextId=' + ctx);
    if (!ctx) throw new Error('找不到 iframe 的执行上下文');
    await cdp.evaluate(`window.parent.postMessage({ type: 'smartlabhub:open-lis-organizer' }, '*'); 'sent'`, 0, ctx);
    await sleep(1500);
    const lisVisible = await cdp.evaluate(`!!document.querySelector('.lis-workspace')`);
    check('唤起 LIS 数据整理', lisVisible === true, '面板=' + lisVisible);
    if (!lisVisible) throw new Error('LIS 面板没出来');

    // ---- 打开「标准字段设置」----
    const dlg = await cdp.evaluate(`(async () => {
      const btn = Array.from(document.querySelectorAll('button')).find(b => b.textContent.includes('标准字段设置'));
      if (!btn) return 'no-button';
      btn.click();
      await new Promise(r => setTimeout(r, 1200));
      const mask = document.querySelector('.slh-modal-mask');
      const rows = document.querySelectorAll('.slh-fs-table tbody tr');
      const heads = Array.from(document.querySelectorAll('.slh-fs-table thead th')).map(t => t.innerText.trim());
      return JSON.stringify({ open: !!mask, rows: rows.length, heads,
        head: document.querySelector('.slh-fs-count')?.innerText.replace(/\\s+/g,' ') || '' });
    })()`);
    const d = JSON.parse(dlg);
    check('设置弹窗能打开', d.open, d.head);
    check('表格显示当前字段数', d.rows > 0, d.rows + ' 行');
    check('已取消「英文字段名」「分组」两列',
      !d.heads.includes('英文字段名') && !d.heads.includes('分组'), d.heads.filter(Boolean).join(' | '));

    // 现有配置是用户真实设置，跑完必须原样还原（不能恢复出厂）
    const before = await (await fetch('http://127.0.0.1:8000/api/data-analysis/lis/fields')).json();
    const n0 = before.fields.length;
    const idx = Math.max(0, before.fields.findIndex(f => f.key === 'barcode'));
    const origLabel = before.fields[idx].label;
    const TMP_KEY = 'verify_tmp_field';


    // ---- 改中文名 + 设必填 + 新增字段 + 保存 ----
    const edited = await cdp.evaluate(`(async () => {
      const rows = () => Array.from(document.querySelectorAll('.slh-fs-table tbody tr'));
      const target = rows()[${idx}];
      if (!target) return JSON.stringify({ fatal: 'no-row' });
      const label = target.querySelector('input.slh-fs-in');
      label.value = ${JSON.stringify(origLabel)} + '(改)';
      label.dispatchEvent(new Event('input', {bubbles:true}));
      const req = target.querySelector('input[type=checkbox]');
      req.checked = true; req.dispatchEvent(new Event('change', {bubbles:true}));
      // 新增一行（英文字段名 / 分组只在新增行里出现）
      const addBtn = Array.from(document.querySelectorAll('.slh-modal button')).find(b => b.textContent.includes('新增字段'));
      addBtn.click();
      await new Promise(r => setTimeout(r, 300));
      const list = rows();
      const ins = list[list.length - 1].querySelectorAll('input.slh-fs-in');
      // 顺序：中文名 / 英文字段名 / 分组 / 说明 / 源列名
      ins[0].value = '验证临时字段'; ins[0].dispatchEvent(new Event('input', {bubbles:true}));
      ins[1].value = '${TMP_KEY}'; ins[1].dispatchEvent(new Event('input', {bubbles:true}));
      ins[2].value = '验证分组'; ins[2].dispatchEvent(new Event('input', {bubbles:true}));
      const saveBtn = Array.from(document.querySelectorAll('.slh-modal button')).find(b => b.textContent.trim() === '保存');
      saveBtn.click();
      await new Promise(r => setTimeout(r, 2000));
      const err = document.querySelector('.slh-modal .form-error');
      const ok = document.querySelector('.slh-fs-ok');
      return JSON.stringify({ error: err ? err.innerText : '', notice: ok ? ok.innerText : '', rows: rows().length });
    })()`);
    const e = JSON.parse(edited);
    check('保存无报错', !e.error, e.error || e.notice);

    // ---- 后端确认 ----
    const st = await (await fetch('http://127.0.0.1:8000/api/data-analysis/lis/fields')).json();
    const byKey = Object.fromEntries(st.fields.map(f => [f.key, f]));
    check('改名已生效', byKey.barcode && byKey.barcode.label === origLabel + '(改)', byKey.barcode && byKey.barcode.label);
    check('必填已生效', byKey.barcode && byKey.barcode.required === true, '');
    check('新增字段已生效', !!byKey[TMP_KEY], byKey[TMP_KEY] ? byKey[TMP_KEY].label : '没找到');
    check('字段总数 +1', st.fields.length === n0 + 1, n0 + ' → ' + st.fields.length);
    check('标记为已自定义', st.customized === true, '');

    // ---- 标记删除：点一次画横线，再点一次撤销，保存后才真正删 ----
    const marked = await cdp.evaluate(`(async () => {
      const rows = () => Array.from(document.querySelectorAll('.slh-fs-table tbody tr'));
      const list = rows();
      const row = list[list.length - 1];
      const btn = () => row.querySelector('td:last-child button');
      if (!btn()) return JSON.stringify({ fatal: 'no-delete-button' });
      const beforeText = btn().textContent.trim();
      btn().click();
      await new Promise(r => setTimeout(r, 250));
      const markedOnce = row.classList.contains('is-marked');
      const afterText = btn().textContent.trim();
      const tip = (document.querySelector('.slh-fs-marked-tip')?.innerText || '').replace(/\\s+/g, ' ').trim();
      const strike = getComputedStyle(row).backgroundImage.includes('gradient');
      btn().click();   // 再点一次 = 撤销
      await new Promise(r => setTimeout(r, 250));
      const unmarked = !row.classList.contains('is-marked');
      const undoText = btn().textContent.trim();
      const tipGone = !document.querySelector('.slh-fs-marked-tip');
      btn().click();   // 重新标记，准备保存
      await new Promise(r => setTimeout(r, 250));
      const saveBtn = Array.from(document.querySelectorAll('.slh-modal button')).find(b => b.textContent.trim() === '保存');
      if (!saveBtn || saveBtn.disabled) return JSON.stringify({ fatal: 'save-disabled' });
      saveBtn.click();
      await new Promise(r => setTimeout(r, 2200));
      return JSON.stringify({ beforeText, markedOnce, afterText, tip, strike, unmarked, undoText, tipGone,
        notice: (document.querySelector('.slh-fs-ok')?.innerText || '').trim(), rows: rows().length });
    })()`);
    const mk = JSON.parse(marked);
    check('点删除只是画横线', mk.markedOnce === true && mk.afterText === '撤销',
      `行标记=${mk.markedOnce} 按钮=${mk.afterText}`);
    check('横线可见', mk.strike === true, '整行中线=' + mk.strike);
    check('待删提示出现', !!mk.tip, mk.tip || '');
    check('再点一次可撤销', mk.unmarked === true && mk.undoText === '删除' && mk.tipGone === true,
      `取消=${mk.unmarked} 按钮=${mk.undoText}`);

    const st2 = await (await fetch('http://127.0.0.1:8000/api/data-analysis/lis/fields')).json();
    check('保存后标记的字段才被删除', !st2.fields.some(f => f.key === TMP_KEY), '剩 ' + st2.fields.length + ' 个');
    check('字段总数回到初始', st2.fields.length === n0, n0 + ' → ' + st2.fields.length);
    check('保存提示带删除数', /删除\s*1\s*个字段/.test(mk.notice || ''), mk.notice || '');

    // ---- 还原成运行前的配置（绝不能把用户真实设置恢复成出厂）----
    if (before.customized) {
      await fetch('http://127.0.0.1:8000/api/data-analysis/lis/fields', {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ fields: before.fields }),
      });
    } else {
      await fetch('http://127.0.0.1:8000/api/data-analysis/lis/fields/reset', { method: 'POST' });
    }
    const st3 = await (await fetch('http://127.0.0.1:8000/api/data-analysis/lis/fields')).json();
    const same = st3.fields.length === n0
      && st3.fields.every((f, i) => f.key === before.fields[i].key && f.label === before.fields[i].label);
    check('还原为运行前配置', same, st3.fields.length + ' 个 / customized=' + st3.customized);

    // 截图
    await cdp.evaluate(`document.querySelector('.slh-modal-mask') && (document.querySelector('.slh-modal-mask').style.display='flex')`);
    await sleep(500);
    const shot = await cdp.send('Page.captureScreenshot', { format: 'png' });
    writeFileSync(OUT, Buffer.from(shot.result.data, 'base64'));
    console.log('\n  截图：' + OUT);

    const errs = cdp.errors.filter((e) => !/favicon|ERR_/.test(e));
    check('页面无 JS 报错', errs.length === 0, errs.slice(0, 2).join(' | '));
  } finally {
    try { ws && ws.close(); } catch {}
    chrome.kill();
  }

  const failed = results.filter((r) => !r.ok);
  console.log('\n' + (failed.length ? '存在失败：' + failed.length : '全部通过 ✅'));
  process.exit(failed.length ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
