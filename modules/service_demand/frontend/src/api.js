// 统一 API 封装：后端一律返回 {ok, data|message}
//
// 这个前端有两种运行方式：
//   1. 独立跑（地址是根，比如 http://127.0.0.1:8100/）→ 前缀为空
//   2. 被 SmartLabHub 嵌在 /portal/ 下（反向代理到本模块）→ 所有 /api 要加前缀
// 不加前缀的话，请求会打到平台自己的接口上（路径对不上，各种 404）。
const BASE = (function () {
  const path = location.pathname // '/portal/' 或 '/portal/index.html'
  const idx = path.indexOf('/portal')
  if (idx < 0) return ''
  const after = path.slice(idx + '/portal'.length)
  // /portal 后面必须紧跟 '/' 或结束，避免 /portaltest 这种误判
  if (after !== '' && !after.startsWith('/')) return ''
  return path.slice(0, idx + '/portal'.length)
})()

/** 给站内绝对路径加上部署前缀，供 fetch 和下载链接共用。 */
export function withBase(path) {
  if (!BASE) return path
  return path.startsWith('/') ? BASE + path : BASE + '/' + path
}

async function req(path, opts = {}) {
  const res = await fetch(withBase(path), {
    headers: { 'Content-Type': 'application/json' },
    ...opts
  })
  let body
  try {
    body = await res.json()
  } catch {
    throw new Error(`服务响应异常 (HTTP ${res.status})`)
  }
  if (!body.ok) throw new Error(body.message || `请求失败 (${res.status})`)
  return body.data
}

export const api = {
  getConfig: () => req('/api/config'),
  saveConfig: (patch) =>
    req('/api/config', { method: 'POST', body: JSON.stringify(patch) }),
  check: () => req('/api/check', { method: 'POST' }),
  meta: () => req('/api/meta'),
  pull: (payload) =>
    req('/api/pull', { method: 'POST', body: JSON.stringify(payload) }),
  pullSample: () => req('/api/pull/sample', { method: 'POST' }),
  history: () => req('/api/history')
}
