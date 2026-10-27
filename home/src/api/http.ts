import axios from 'axios'

export const api = axios.create({
  baseURL: '/api',
  timeout: 5000,
})

/**
 * 登录过期统一兜底。
 *
 * 后端所有 /api/ 接口都要求登录，会话过期后返回 401 + need_login。
 * 这里集中把用户送回登录页，并记住原本要去的地址，登录后跳回去。
 *
 * 两个例外不能跳：
 *  - /auth/* 自身（登录页正在请求，跳了就死循环）
 *  - 已经在登录页/首次设置页时（避免无意义的重定向）
 */
api.interceptors.response.use(
  (response) => response,
  (error) => {
    const status = error?.response?.status
    const url: string = error?.config?.url || ''
    const isAuthCall = url.includes('/auth/')
    const onAuthPage =
      window.location.pathname === '/login' || window.location.pathname === '/setup'

    if (status === 401 && !isAuthCall && !onAuthPage) {
      const redirect = `${window.location.pathname}${window.location.search}`
      window.location.replace(`/login?redirect=${encodeURIComponent(redirect)}`)
    }
    return Promise.reject(error)
  },
)
