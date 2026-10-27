/**
 * 从图片里解出 TOTP 二维码内容。
 *
 * 用途：用户不方便从验证器 App 里翻密钥时，把 Salesforce 的
 * "连接验证器应用"页面二维码截图丢进来，这里直接解出
 * otpauth://totp/...?secret=BASE32... ，从中取 secret 填配置。
 *
 * 用法：
 *   node decode_totp_qr.mjs <图片路径> [更多图片...]
 *
 * 依赖：jsqr（纯 JS 解码）+ pngjs（读 PNG）/ jpeg-js（读 JPG）
 * 都不进项目依赖，只是这个工具的本地依赖。
 */
import { readFileSync, existsSync } from 'node:fs';
import { extname, basename } from 'node:path';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const jsQR = require('jsqr');
const { PNG } = require('pngjs');
const jpeg = require('jpeg-js');

/** 把图片文件解码成 {data: Uint8ClampedArray, width, height} */
function loadImage(file) {
  const buf = readFileSync(file);
  const ext = extname(file).toLowerCase();
  if (ext === '.png') {
    const png = PNG.sync.read(buf);
    return { data: png.data, width: png.width, height: png.height };
  }
  if (ext === '.jpg' || ext === '.jpeg') {
    const img = jpeg.decode(buf, { useTArray: true });
    return { data: new Uint8ClampedArray(img.data), width: img.width, height: img.height };
  }
  throw new Error(`暂不支持的图片格式：${ext}（只支持 .png / .jpg）`);
}

/** 从 otpauth:// 链接里取 base32 密钥 */
function parseOtpAuth(uri) {
  const out = { secret: '', issuer: '', account: '', algorithm: '', digits: '', period: '' };
  const m = /^otpauth:\/\/totp\/([^?]*)\?(.*)$/i.exec(uri);
  if (!m) return out;
  const label = decodeURIComponent(m[1]);
  const colon = label.indexOf(':');
  if (colon >= 0) {
    out.issuer = label.slice(0, colon);
    out.account = label.slice(colon + 1);
  } else {
    out.account = label;
  }
  for (const pair of m[2].split('&')) {
    const [k, v] = pair.split('=');
    if (!k) continue;
    const key = k.toLowerCase();
    if (key in out) out[key] = decodeURIComponent(v || '');
  }
  return out;
}

const files = process.argv.slice(2);
if (!files.length) {
  console.error('用法：node decode_totp_qr.mjs <图片路径> [更多图片...]');
  process.exit(2);
}

for (const file of files) {
  console.log(`=== ${basename(file)} ===`);
  if (!existsSync(file)) {
    console.log('  文件不存在');
    continue;
  }
  let image;
  try {
    image = loadImage(file);
  } catch (err) {
    console.log(`  读取失败：${err.message}`);
    continue;
  }
  const result = jsQR(image.data, image.width, image.height);
  if (!result) {
    console.log('  没解出二维码（图片太糊 / 二维码太小 / 不是二维码）');
    console.log('  建议：截原始分辨率的图，别用聊天软件压缩过的');
    continue;
  }
  const text = result.data;
  console.log(`  原始内容：${text}`);
  if (/^otpauth:\/\/totp\//i.test(text)) {
    const info = parseOtpAuth(text);
    console.log(`  密钥(secret)：${info.secret}`);
    console.log(`  账户：${info.issuer ? info.issuer + ' / ' : ''}${info.account}`);
    console.log(`  参数：algorithm=${info.algorithm || 'SHA1(默认)'} digits=${info.digits || '6(默认)'} period=${info.period || '30(默认)'}`);
    if (!info.secret) {
      console.log('  ⚠ 没解出 secret，可能不是标准 otpauth 二维码');
    }
  } else {
    console.log('  ⚠ 不是 otpauth:// 链接，是别的二维码');
  }
}
