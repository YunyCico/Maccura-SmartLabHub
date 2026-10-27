/**
 * 真实验证「可编辑名称 + 头像」：
 *   1. 登录（用工号 95，2 位账号，验证短工号也能进）
 *   2. 进 /account，改显示名称 -> 顶栏立刻变
 *   3. 用**真实文件选择**上传头像 -> 保存 -> 顶栏出现头像图片
 *   4. 校验上传后存的是压缩过的小图（原图 121KB，压完应在 20KB 内）
 *   5. 清除头像 -> 回退成姓名首字
 *
 * 用法：node platform/tools/verify_profile_edit.mjs
 */
import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync, statSync, writeFileSync } from 'node:fs';
import zlib from 'node:zlib';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..', '..');
const CHROME = path.join(process.env.LOCALAPPDATA || '', 'Google/Chrome/Application/chrome.exe');
const PYTHON = process.env.SLH_PYTHON || 'C:\\Users\\98319\\.workbuddy\\binaries\\python\\envs\\smartlabhub\\Scripts\\python.exe';
const TEST_IMAGE = path.join(HERE, '_test_avatar.png');

/**
 * 现场生成一张 240×240 的渐变 PNG 当测试图。
 * 不把图片放进仓库：二进制文件没必要版本管理，脚本自己造就行。
 */
function ensureTestImage() {
  if (existsSync(TEST_IMAGE)) return;
  const W = 240, H = 240;
  const rows = [];
  for (let y = 0; y < H; y++) {
    const row = Buffer.alloc(1 + W * 3); // 每行开头是 filter type 0
    for (let x = 0; x < W; x++) {
      const i = 1 + x * 3;
      row[i] = Math.round((x * 255) / W);
      row[i + 1] = 180;
      row[i + 2] = Math.round((y * 255) / H);
    }
    rows.push(row);
  }
  const chunk = (tag, data) => {
    const len = Buffer.alloc(4);
    len.writeUInt32BE(data.length);
    const body = Buffer.concat([Buffer.from(tag, 'ascii'), data]);
    const crc = Buffer.alloc(4);
    crc.writeUInt32BE(zlib.crc32 ? zlib.crc32(body) : 0);
    return Buffer.concat([len, body, crc]);
  };
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(W, 0);
  ihdr.writeUInt32BE(H, 4);
  ihdr[8] = 8;  // bit depth
  ihdr[9] = 2;  // color type: truecolor
  const png = Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr),
    chunk('IDAT', zlib.deflateSync(Buffer.concat(rows), { level: 9 })),
    chunk('IEND', Buffer.alloc(0)),
  ]);
  writeFileSync(TEST_IMAGE, png);
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitHttp(url, timeoutMs) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    try {
      const response = await fetch(url, { signal: AbortSignal.timeout(2500) });
      if (response.ok) return await response.text();
    } catch {}
    await sleep(400);
  }
  return null;
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

async function getJson(url) {
  try {
    const response = await fetch(url, { signal: AbortSignal.timeout(3000) });
    return await response.json();
  } catch { return null; }
}

const results = [];
function check(name, ok, extra = '') {
  results.push({ name, ok, extra });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${extra ? '  ' + extra : ''}`);
}

async function main() {
  if (!existsSync(CHROME)) return console.log('SKIP chrome missing'), 0;
  if (!existsSync(PYTHON)) return console.log('SKIP python missing'), 0;
  ensureTestImage();
  const srcBytes = statSync(TEST_IMAGE).size;
  console.log(`测试原图 ${Math.round(srcBytes / 1024)}KB`);

  const env = { ...process.env, NO_PROXY: '*', no_proxy: '*' };
  for (const key of Object.keys(env)) if (/proxy/i.test(key)) delete env[key];
  const authDir = mkdtempSync(path.join(os.tmpdir(), 'slh_profile_'));
  env.SLH_AUTH_DB = path.join(authDir, 'auth.db');

  let backend = null; let frontend = null; let chrome = null; let profile = null;
  try {
    const backendPort = await freePort();
    const frontendPort = await freePort();
    backend = spawn(PYTHON, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(backendPort), '--app-dir', 'platform/backend'], {
      cwd: ROOT, env, stdio: ['ignore', 'pipe', 'pipe'],
    });
    backend.stderr.on('data', (c) => {
      const t = c.toString();
      if (/Traceback|ERROR/.test(t)) process.stderr.write('[api!] ' + t);
    });
    if (!(await waitHttp(`http://127.0.0.1:${backendPort}/api/health`, 90000))) throw new Error('backend did not come up');

    frontend = spawn(process.execPath, [path.join(ROOT, 'home', 'node_modules', 'vite', 'bin', 'vite.js'), '--host', '127.0.0.1', '--port', String(frontendPort), '--strictPort'], {
      cwd: path.join(ROOT, 'home'),
      env: { ...env, SLH_API_TARGET: `http://127.0.0.1:${backendPort}` },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    if (!(await waitHttp(`http://127.0.0.1:${frontendPort}/`, 90000))) throw new Error('frontend did not come up');

    // 用工号 95（2 位）建管理员，顺带验证短工号
    const seeded = await fetch(`http://127.0.0.1:${backendPort}/api/auth/setup`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: '95', password: '123456000', display_name: '95' }),
    }).then((r) => r.json()).catch(() => null);
    check('用工号 95 建管理员（2 位工号）', Boolean(seeded?.user), seeded?.detail || '');

    const debugPort = await freePort();
    profile = mkdtempSync(path.join(os.tmpdir(), 'slh_profile_chrome_'));
    chrome = spawn(CHROME, [
      `--remote-debugging-port=${debugPort}`, `--user-data-dir=${profile}`,
      '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
      '--no-proxy-server', '--proxy-bypass-list=*', '--window-size=1400,1100', 'about:blank',
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
    await page.send('DOM.enable');

    const BASE = `http://127.0.0.1:${frontendPort}`;
    async function ctx() {
      for (let i = 0; i < 40; i++) {
        const c = [...page.contexts.values()].find((x) => x.origin.includes(`127.0.0.1:${frontendPort}`))?.id;
        if (c) return c;
        await sleep(300);
      }
      return null;
    }
    async function navigate(url) {
      page.contexts.clear();
      await page.send('Page.navigate', { url });
      await sleep(1500);
      try { await page.send('Runtime.disable'); await page.send('Runtime.enable'); } catch {}
      await sleep(600);
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
    async function topBar() {
      return page.evalJson(`(function () {
        var img = document.querySelector('.brand-bar .avatar img');
        var txt = document.querySelector('.brand-bar .user-copy');
        return {
          name: txt ? txt.querySelector('span').textContent.trim() : '',
          role: txt ? txt.querySelector('small').textContent.trim() : '',
          hasAvatarImg: !!img,
          avatarLen: img ? img.getAttribute('src').length : 0,
          avatarKind: img ? img.getAttribute('src').slice(0, 22) : '',
        };
      })()`, await ctx());
    }

    // ---- 登录
    await navigate(BASE + '/login');
    await typeInto('.auth-field:nth-of-type(1) input', '95');
    await typeInto('.auth-form .auth-password input', '123456000');
    await click('.auth-submit');
    await sleep(2000);
    let bar = await topBar();
    check('工号 95 登录成功', bar.name === '95', `name=${bar.name}`);

    // ---- 进账号页改名称
    await navigate(BASE + '/account');
    await sleep(1200);
    await typeInto('.profile-fields input', '孟鹏杰');
    // 点「保存资料」
    await click('.account-card .account-primary');
    await sleep(2000);
    bar = await topBar();
    check('改显示名称后顶栏立刻更新', bar.name === '孟鹏杰', `name=${bar.name}`);

    // ---- 上传头像（真实文件选择）
    const doc = await page.send('DOM.getDocument', { depth: -1 });
    const rootId = doc.result.root.nodeId;
    const inputNode = await page.send('DOM.querySelector', {
      nodeId: rootId, selector: '.account-card .avatar-input',
    });
    if (!inputNode.result.nodeId) throw new Error('找不到头像 file input');
    await page.send('DOM.setFileInputFiles', {
      files: [TEST_IMAGE], nodeId: inputNode.result.nodeId,
    });
    await sleep(1500);

    const previewLen = await page.evalJson(`(function () {
      var img = document.querySelector('.account-card .avatar-wrap img');
      return img ? img.getAttribute('src').length : 0;
    })()`, await ctx());
    check('选图后出现头像预览', previewLen > 100, `dataUrl 长度=${previewLen}`);
    // 121KB 的原图压缩后应该只有几 KB（base64 约 1.37 倍）
    check('前端已压缩（data URL < 20KB）', previewLen < 20 * 1024, `${Math.round(previewLen / 1024)}KB`);

    await click('.account-card .account-primary');
    await sleep(2000);
    bar = await topBar();
    check('保存后顶栏显示头像图片', bar.hasAvatarImg, `kind=${bar.avatarKind}`);
    check('存的是 webp/png 的 data URL', /^data:image\//.test(bar.avatarKind), bar.avatarKind);
    check('顶栏名称仍是新名字', bar.name === '孟鹏杰', `name=${bar.name}`);

    // ---- 清除头像
    await navigate(BASE + '/account');
    await sleep(1200);
    await page.evalJson(`(function () {
      var btn = document.querySelector('.account-card .avatar-clear');
      if (btn) btn.click();
      return true;
    })()`, await ctx());
    await sleep(600);
    await click('.account-card .account-primary');
    await sleep(1800);
    bar = await topBar();
    check('清除头像后回退成姓名首字', !bar.hasAvatarImg && bar.name === '孟鹏杰', `hasImg=${bar.hasAvatarImg}`);

    // ---- 截图
    try {
      const shot = await page.send('Page.captureScreenshot', { format: 'png' });
      const shotPath = path.join(os.tmpdir(), 'slh_profile_edit.png');
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
    try { frontend?.kill(); } catch {}
    try { backend?.kill(); } catch {}
    try { profile && rmSync(profile, { recursive: true, force: true }); } catch {}
    try { authDir && rmSync(authDir, { recursive: true, force: true }); } catch {}
  }
}

process.exit(await main());
