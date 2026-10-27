/**
 * 真实交互验证：TAT 分析区块的三处新功能。
 *   ① 参数区只显示"本次计算用到的字段"（不再有两个空下拉框）
 *   ② 四个分段 + 实验室内 TAT + ToTAT 都算得出来
 *   ③ TAT 标准设置能保存，分析后能按「专业 × 平诊/急诊」判超标
 *
 * 用法：node platform/tools/verify_tat_ui.mjs
 * 前置：平台已启动（127.0.0.1:8765 是原生引擎，:8000 是平台后端）。
 */
import { spawn } from 'node:child_process';
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..', '..');
const CHROME = path.join(process.env.LOCALAPPDATA || '', 'Google/Chrome/Application/chrome.exe');
const ENGINE = 'http://127.0.0.1:8765';
const OUT = path.join(ROOT, 'platform', 'tools', '_tat_ui.png');
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
    ws.addEventListener('message', (ev) => {
      const m = JSON.parse(ev.data);
      if (m.id && this.pending.has(m.id)) {
        const p = this.pending.get(m.id); this.pending.delete(m.id); p.resolve(m); return;
      }
      if (m.method === 'Runtime.exceptionThrown') {
        const d = m.params?.exceptionDetails || {};
        this.errors.push(d.exception?.description || d.text || 'runtime exception');
      }
    });
  }
  send(method, params = {}, timeoutMs = 90000) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error('timeout ' + method)); }, timeoutMs);
      this.pending.set(id, { resolve: (m) => { clearTimeout(timer); resolve(m); } });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  async evaluate(expression) {
    const r = await this.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
    if (r?.result?.exceptionDetails) {
      throw new Error('页面异常: ' + (r.result.exceptionDetails.exception?.description || r.result.exceptionDetails.text));
    }
    const v = r?.result?.result?.value;
    if (v === undefined) throw new Error('evaluate 无返回值: ' + JSON.stringify(r).slice(0, 400));
    return v;
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
    try { const r = await fetch(url, { signal: AbortSignal.timeout(2500) }); if (r.ok) return await r.text(); } catch {}
    await sleep(400);
  }
  return null;
}

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok, detail });
  console.log(`  ${ok ? '✅' : '❌'} ${name}${detail ? ' — ' + detail : ''}`);
}

async function main() {
  if (!existsSync(CHROME)) { console.error('Chrome 不存在'); process.exit(2); }
  if (!await waitHttp(ENGINE + '/api/state', 8000)) { console.error('引擎未启动（8765）'); process.exit(2); }

  // ★ 本脚本会改 TAT 标准并保存 —— 用户在界面上设的是真实口径，跑完必须原样还原
  const STD_FILE = path.join(ROOT, 'modules', 'data_analysis', 'data', 'smartlab', 'library', 'config', 'tat_standards.json');
  const stdBackup = existsSync(STD_FILE) ? readFileSync(STD_FILE, 'utf8') : null;
  const restoreStandards = () => {
    try {
      if (stdBackup === null) return;
      writeFileSync(STD_FILE, stdBackup, 'utf8');
    } catch {}
  };

  const port = await freePort();
  const profile = path.join(os.tmpdir(), 'slh_tat_profile_' + Date.now());
  const chrome = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--hide-scrollbars', '--no-first-run',
    '--remote-debugging-port=' + port, '--user-data-dir=' + profile,
    '--window-size=1500,1200', 'about:blank',
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
    await cdp.send('Page.enable');
    await cdp.send('Runtime.enable');

    await cdp.send('Page.navigate', { url: ENGINE + '/' });
    await sleep(2500);

    // ---- 切到「数据分析」页（数据表列表是切页后才渲染的）----
    const navd = await cdp.evaluate(`(async () => {
      try {
        const link = document.querySelector('[data-page="analysis"]');
        if (link) link.click();
        await new Promise(r => setTimeout(r, 1500));
        return 'ok:' + (document.querySelector('#page-analysis')?.classList.contains('active') ? 'active' : 'not-active');
      } catch (e) { return 'err:' + (e && e.message); }
    })()`);
    check('切到数据分析页', String(navd).startsWith('ok'), String(navd));

    // ---- 勾选一张**小**的 TAT 数据表 → 生成 TAT 数据 ----
    //   （生成 TAT 数据其实是在做汇总提取，勾到大表会很慢）
    console.log('\n[1] 生成 TAT 数据');
    const gen = await cdp.evaluate(`(async () => {
      const items = Array.from(document.querySelectorAll('#tat-ds-list .pick, #tat-ds-list > div, #tat-ds-list label'));
      const cbs = Array.from(document.querySelectorAll('#tat-ds-list input[type=checkbox]'));
      if (!cbs.length) return '没有可选的数据表';
      // 选行数最少的那张（文本里的行数）
      let best = 0, bestRows = Infinity;
      cbs.forEach((cb, i) => {
        const box = cb.closest('div,label,pick') || cb.parentElement;
        const txt = (box && box.innerText) || '';
        const m = txt.match(/([\\d,]+)\\s*行/);
        const n = m ? Number(m[1].replace(/,/g,'')) : Infinity;
        if (n < bestRows) { bestRows = n; best = i; }
      });
      cbs[best].checked = true;
      cbs[best].dispatchEvent(new Event('change', {bubbles:true}));
      const name = (cbs[best].closest('div,label,pick')||cbs[best].parentElement)?.innerText.replace(/\\s+/g,' ').slice(0,60) || '';
      document.querySelector('#tat-ds-run').click();
      for (let i=0;i<280;i++){
        await new Promise(r=>setTimeout(r,500));
        if (S.tatResult && S.tatResult.id) return 'ok:' + S.tatResult.id + ' | ' + name;
      }
      return '超时 | ' + name;
    })()`);
    check('生成 TAT 数据', String(gen).startsWith('ok:'), String(gen));
    if (!String(gen).startsWith('ok:')) throw new Error('TAT 数据没生成出来');

    // ---- 点「计算」 ----
    console.log('\n[2] 计算（分段 + 标准）');
    const run = await cdp.evaluate(`(async () => {
      document.querySelector('#tat-run').click();
      for (let i=0;i<120;i++){
        await new Promise(r=>setTimeout(r,500));
        if (S.tatLast) return 'ok';
      }
      return '超时';
    })()`);
    check('TAT 计算完成', run === 'ok', String(run));
    if (run !== 'ok') throw new Error('TAT 计算没完成');

    const info = await cdp.evaluate(`(() => {
      const fields = document.querySelector('#tat-fields');
      const body = document.querySelector('#tat-body');
      return JSON.stringify({
        fields: fields ? fields.innerText.replace(/\\s+/g,' ') : '',
        chips: fields ? fields.querySelectorAll('.tat-fchip').length : 0,
        missing: fields ? fields.querySelectorAll('.tat-fchip.miss').length : 0,
        segs: body ? body.querySelectorAll('.tat-seg').length : 0,
        blocks: body ? body.querySelectorAll('.tat-block').length : 0,
        hasStd: !!(body && body.querySelector('.tat-std-block')),
        text: body ? body.innerText.replace(/\\s+/g,' ').slice(0, 700) : ''
      });
    })()`);
    const d = JSON.parse(info);
    check('参数区显示字段清单', d.chips > 0, d.fields.slice(0, 120));
    check('不再有空下拉框', await cdp.evaluate(`!document.querySelector('#tat-start') && !document.querySelector('#tat-end')`), 'tat-start/tat-end 已移除');
    check('四个分段区块', d.segs >= 2, '找到 ' + d.segs + ' 个分段卡（缺某个环节时会更少）');
    check('指标区块数', d.blocks >= 2, d.blocks + ' 个');
    check('标准判定区块存在', d.hasStd, '');
    console.log('    页面文案：' + d.text.slice(0, 320));

    // ---- 标准设置弹窗 ----
    console.log('\n[3] TAT 标准设置');
    const std = await cdp.evaluate(`(async () => {
      document.querySelector('#tat-std').click();
      await new Promise(r=>setTimeout(r,800));
      const rows = document.querySelectorAll('#tat-std-rows tr');
      const inputs = document.querySelectorAll('.tat-std-in');
      return JSON.stringify({ rows: rows.length, inputs: inputs.length,
        open: document.querySelector('#tat-std-modal').classList.contains('show'),
        specs: Array.from(rows).map(r=>r.querySelector('b')?r.querySelector('b').innerText:'?') });
    })()`);
    const s = JSON.parse(std);
    check('标准弹窗能打开', s.open, '');
    check('四个专业都有输入', s.rows === 4 && s.inputs === 8, s.specs.join('、'));

    // 填两个值并保存（保存走页面同源的 8765 引擎端点）
    await cdp.evaluate(`(async () => {
      const set = (spec, urg, v) => {
        const el = document.querySelector('.tat-std-in[data-spec="'+spec+'"][data-urg="'+urg+'"]');
        if (el) { el.value = v; }
      };
      set('生化','平诊','4'); set('生化','急诊','2');
      set('免疫','平诊','8');
      document.querySelector('#tat-std-save').click();
      await new Promise(r=>setTimeout(r,1500));
      return 'saved';
    })()`);
    // 在 Node 侧读回（跨域 fetch 只能从 Node 发）
    let saved = '读取失败';
    try {
      const res = await fetch('http://127.0.0.1:8000/api/data-analysis/tat/standards');
      const j = await res.json();
      saved = JSON.stringify(j.standards ? j.standards.limits['生化'] : j).slice(0, 200);
    } catch (e) { saved = 'err: ' + e.message; }
    check('标准能保存并读回', saved.includes('4') && saved.includes('2'), saved);

    // ---- 「恢复默认」：出厂口径 = 生化 4/2、免疫 4/2、血常规 0.5/0.25、凝血 2/1 ----
    const dft = await cdp.evaluate(`(async () => {
      const btn = document.querySelector('#tat-std-default');
      if (!btn) return JSON.stringify({ fatal: 'no-button' });
      btn.click();
      await new Promise(r=>setTimeout(r,300));
      const vals = {};
      document.querySelectorAll('.tat-std-in').forEach(i => { vals[i.dataset.spec + '/' + i.dataset.urg] = i.value; });
      return JSON.stringify({ vals, target: (document.querySelector('#tat-std-target')||{}).value });
    })()`);
    const df = JSON.parse(dft);
    const want = { '生化/平诊': '4', '生化/急诊': '2', '免疫/平诊': '4', '免疫/急诊': '2',
                   '血常规/平诊': '0.5', '血常规/急诊': '0.25', '凝血/平诊': '2', '凝血/急诊': '1' };
    check('恢复默认填出出厂口径', !!df.vals && Object.keys(want).every(k => String(df.vals[k]) === want[k]),
      JSON.stringify(df.vals));
    check('恢复默认把判定对象设回实验室内 TAT', df.target === 'lab_tat', String(df.target));

    // 再算一次看超标判定
    const over = await cdp.evaluate(`(async () => {
      document.querySelector('#tat-std-x').click();
      document.querySelector('#tat-run').click();
      for (let i=0;i<80;i++){ await new Promise(r=>setTimeout(r,500)); if (S.tatLast && S.tatLast.standard) break; }
      const b = document.querySelector('#tat-body');
      return JSON.stringify({ hasStd: !!(b && b.querySelector('.tat-std-block')),
        text: b ? b.innerText.replace(/\\s+/g,' ').slice(0,500) : '' });
    })()`);
    const o = JSON.parse(over);
    check('超标判定区块', o.hasStd, o.text.slice(0, 240));

    // 截图
    await cdp.evaluate(`document.querySelector('#tat-card').scrollIntoView()`);
    await sleep(600);
    const shot = await cdp.send('Page.captureScreenshot', { format: 'png' });
    writeFileSync(OUT, Buffer.from(shot.result.data, 'base64'));
    console.log('\n  截图：' + OUT);

    const errs = cdp.errors.filter((e) => !/favicon|ERR_/.test(e));
    check('页面无 JS 报错', errs.length === 0, errs.slice(0, 2).join(' | '));
  } finally {
    try { ws && ws.close(); } catch {}
    chrome.kill();
    restoreStandards();      // 把用户原来的 TAT 标准写回去
  }

  const failed = results.filter((r) => !r.ok);
  console.log('\n' + (failed.length ? '存在失败：' + failed.length : '全部通过 ✅'));
  process.exit(failed.length ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
