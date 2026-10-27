import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// 后端没起来时，代理默认会回一段 "ECONNREFUSED" 的纯文本。
// 但免登跳转是**在新标签页**里发生的，用户看到的就是一屏技术报错。
// 这里换成可读的一页，并直接告诉用户该怎么恢复。
const backendDownPage = `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>后端服务未启动</title>
<style>
 body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
      background:#f5f6f8;font:14px/1.7 "Microsoft YaHei",-apple-system,sans-serif;color:#2c2c2a}
 .box{max-width:540px;background:#fff;border:1px solid #e5e5ea;border-radius:14px;padding:32px 34px;
       box-shadow:0 2px 10px rgba(0,0,0,.05)}
 h1{margin:0 0 12px;font-size:18px;font-weight:500}
 p{margin:8px 0;color:#55555c}
 code{background:#f2f2f5;border-radius:5px;padding:2px 7px;font-size:12.5px}
 a{display:inline-block;margin-top:18px;background:#185fa5;color:#fff;text-decoration:none;
   padding:9px 20px;border-radius:999px;font-size:13px}
</style></head>
<body><div class="box">
<h1>后端服务未启动</h1>
<p>工作平台的后端进程没在运行，所以这次跳转没能完成。</p>
<p>请运行 <code>停止平台.bat</code> 再运行 <code>启动平台.bat</code>，让平台重新加载最新代码。</p>
<a href="/">← 返回工作平台</a>
</div></body></html>`

const target = process.env.SLH_API_TARGET || 'http://localhost:8000'

/** 后端不可用时给一页人能看懂的提示，而不是 ECONNREFUSED。
 *
 * 注意：Vite 的 `configure` 要求参数是它自己的 ProxyServer 类型，
 * 这里只用到 `.on('error', ...)`，用宽松签名避免被类型系统挡住。
 */
function readableProxyError(proxy: {
  on: (event: string, handler: (err: unknown, req: unknown, res: unknown) => void) => void
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
}): void {
  proxy.on('error', (_err, _req, res) => {
    // 代理错误时 res 可能是 Socket，这里只处理能写 HTTP 响应的那种
    const response = res as {
      writeHead?: (code: number, headers: Record<string, string>) => void
      end?: (body: string) => void
    }
    if (typeof response.writeHead === 'function' && typeof response.end === 'function') {
      response.writeHead(502, { 'Content-Type': 'text/html; charset=utf-8' })
      response.end(backendDownPage)
    }
  })
}

// Vite 期望 configure 的签名是 (proxy: ProxyServer, options) => void，
// 我们这个函数只接收第一个参数，运行时完全兼容；
// 但 TS 的形参逆变不接受窄化签名，所以这里显式放宽成 any。
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const configure: any = readableProxyError

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  server: {
    proxy: {
      '/api': { target, configure },
      // 数据分析工作台通过 /engine/ 内嵌（原来是直连 127.0.0.1:8765，
      // 部署到服务器后那个地址指的是用户自己的电脑，必然白屏）。
      // 本地开发也必须一起代理，否则 5173 上点「数据分析」会 404。
      '/engine': { target, configure },
      // 服务需求报表通过 /portal/ 内嵌。这条**漏了会套娃**：
      // Vite 的 SPA 兜底会把平台自己的 index.html 返回给 iframe，
      // 看起来就是"平台里套了一个平台"。
      '/portal': { target, configure },
    },
  },
})
