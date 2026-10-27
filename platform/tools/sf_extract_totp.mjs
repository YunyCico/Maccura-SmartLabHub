/**
 * 从 Salesforce 页面自动取出 TOTP 密钥。
 *
 * 背景：账号开了 TOTP 双因素认证，密钥只有登录后在
 * 「高级用户详情 → App Registration: One-Time Password Authenticator → Connect」
 * 页面里能看到（点 "I Can't Scan the QR Code" 会显示明文 security key）。
 * 手动去翻很麻烦，这个脚本直接开一个可视 Chrome：
 *   1. 你在窗口里正常登录（密码 + 腾讯身份验证器的 6 位码）
 *   2. 脚本检测到登录成功，自动跳到该页面
 *   3. 页面上的密钥 / 二维码由脚本自动读出并打印
 *
 * 用法：node platform/tools/sf_extract_totp.mjs
 * 依赖：只用到 node 内置模块 + 上面同目录的 jsqr/pngjs（解二维码用）
 */
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, writeFileSync, readFileSync } from 'node:fs';
import path from 'node:path';
import http from 'node:http';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..', '..');
const CHROME = path.join(process.env.LOCALAPPDATA || '', 'Google/Chrome/Application/chrome.exe');
const CDP_PORT = 9361;
const PROFILE = path.join(ROOT, 'shots', '_cdp_sf_totp');
const OUT_PNG = path.join(ROOT, 'shots', 'sf_totp_qr.png');

const HOST = process.env.SF_HOST || 'maccuracn.my.sfcrmproducts.cn';
const LOGIN_URL = `https://${HOST}/`;
// 登录后要去的页面（Lightning 的高级用户详情，含一次性口令验证器注册）
const TARGETS = [
  'https://maccuracn.lightning.sfcrmapps.cn/lightning/setup/AdvancedUserDetails/home',
  `https://${HOST}/userprofile/manageTwoFactorAuthentication`,
  `https://${HOST}/servlet/servlet.su?oid=00DC8000009rygr&retURL=%2Fuserprofile%2FmanageTwoFactorAuthentication`,
];

const log = (...a) => console.log('[sf-totp]', ...a);
const sleep = ms => new Promise(r => setTimeout(r, ms));

function get(url) {
  return new Promise((res, rej) => {
    http.get(url, r => {
      let b = '';
      r.on('data', c => (b += c));
      r.on('end', () => { try { res(JSON.parse(b)); } catch (e) { rej(e); } });
    }).on('error', rej);
  });
}

/** 从页面文本里捞 base32 密钥 */
function pickSecretFromText(text) {
  // 常见形态：单独一行的大写 base32，或 "Key"/"密钥" 后面跟的一串
  const labeled = /(?:setup\s*key|security\s*key|密钥|Key)\s*[:：]?\s*([A-Z2-7]{16,64})/i.exec(text);
  if (labeled) return labeled[1];
  const bare = /\b([A-Z2-7]{16,64})\b/.exec(text);
  if (bare) return bare[1];
  return '';
}

(async () => {
  if (!existsSync(CHROME)) { log('SKIP: 未找到 Chrome'); return 0; }
  mkdirSync(PROFILE, { recursive: true });
  // 不加 --headless：需要你看到窗口并手动登录
  const chrome = spawn(CHROME, [
    `--remote-debugging-port=${CDP_PORT}`, `--user-data-dir=${PROFILE}`,
    '--no-first-run', '--no-default-browser-check', '--disable-gpu',
    '--window-size=1280,900', LOGIN_URL,
  ], { stdio: 'ignore', detached: false });

  let ws;
  try {
    let targets = null;
    for (let i = 0; i < 60; i++) {
      try { targets = await get(`http://127.0.0.1:${CDP_PORT}/json/list`); break; }
      catch { await sleep(500); }
    }
    if (!targets) { log('ERROR: 连不上 Chrome 调试端口'); return 1; }
    const page = targets.find(t => t.type === 'page');
    if (!page) { log('ERROR: 没有可用页面'); return 1; }

    ws = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise(r => (ws.onopen = r));
    let id = 0;
    const pend = new Map();
    ws.onmessage = e => {
      const m = JSON.parse(e.data);
      if (m.id && pend.has(m.id)) { pend.get(m.id)(m.result); pend.delete(m.id); }
    };
    const send = (method, params = {}) => new Promise(r => {
      const i = ++id; pend.set(i, r); ws.send(JSON.stringify({ id: i, method, params }));
    });
    const evaluate = async expr =>
      (await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true }))?.result?.value;

    await send('Page.enable');
    await send('Runtime.enable');

    log('Chrome 已打开，请在窗口里用「密码 + 腾讯身份验证器 6 位码」登录 Salesforce。');
    log('（登录成功后本脚本会自动继续，不用管窗口）');

    // 等待登录成功：不再出现登录表单，也不在 TOTP 验证页
    let loggedIn = false;
    for (let i = 0; i < 180; i++) {
      await sleep(2000);
      const state = await evaluate(`(function(){
        var u = location.href;
        var hasUser = !!document.querySelector('input[name="username"]');
        var hasTotp = !!document.querySelector('input[name="tc"]');
        var body = (document.body && document.body.innerText || '').slice(0, 400);
        return JSON.stringify({url: u, hasUser: hasUser, hasTotp: hasTotp, body: body});
      })()`);
      if (!state) continue;
      const s = JSON.parse(state);
      if (s.hasTotp) {
        if (i % 5 === 0) log('  等你填 6 位动态口令…');
        continue;
      }
      if (s.hasUser) {
        if (i % 5 === 0) log('  等你完成登录…');
        continue;
      }
      loggedIn = true;
      log('登录成功，当前页面：', s.url.slice(0, 90));
      break;
    }
    if (!loggedIn) { log('超时未检测到登录成功'); return 1; }

    // 逐个尝试目标页面，找带二维码/密钥的那个
    for (const target of TARGETS) {
      log('打开：', target.slice(0, 95));
      await send('Page.navigate', { url: target });
      await sleep(6000);
      const info = await evaluate(`(function(){
        var body = document.body ? document.body.innerText : '';
        var imgs = Array.prototype.map.call(document.querySelectorAll('img'), function(i){
          return {src: (i.currentSrc || i.src || ''), alt: i.alt || '', w: i.naturalWidth, h: i.naturalHeight};
        });
        return JSON.stringify({url: location.href, body: body.slice(0, 4000), imgs: imgs});
      })()`);
      if (!info) continue;
      const data = JSON.parse(info);
      const secret = pickSecretFromText(data.body);
      if (secret) {
        log('');
        log('=== 找到 TOTP 密钥 ===');
        log(secret);
        log('');
        log('把它填到 SmartLabHub 设置页的「TOTP 密钥（动态口令）」里即可。');
        return 0;
      }
      // 找二维码图片
      const qr = (data.imgs || []).find(i => i.w >= 100 && i.h >= 100 && /data:image|^https?:/.test(i.src));
      if (qr) {
        log('  页面有图片，尝试取二维码：', (qr.alt || qr.src).slice(0, 60));
        const b64 = await evaluate(`(function(){
          var imgs = Array.prototype.slice.call(document.querySelectorAll('img'));
          for (var i = 0; i < imgs.length; i++) {
            var s = imgs[i].currentSrc || imgs[i].src || '';
            if (s.indexOf('data:image') === 0) return s.split(',')[1];
          }
          return '';
        })()`);
        if (b64) {
          writeFileSync(OUT_PNG, Buffer.from(b64, 'base64'));
          log('二维码已保存：', OUT_PNG);
          log('现在运行：node platform/tools/decode_totp_qr.mjs', OUT_PNG);
          return 0;
        }
      }
      log('  这个页面没找到密钥/二维码');
    }
    log('所有候选页面都没取到密钥，请把页面截图发出来人工看');
    return 1;
  } catch (err) {
    log('ERROR:', err.message);
    return 1;
  } finally {
    if (ws) { try { ws.close(); } catch {} }
  }
})();
