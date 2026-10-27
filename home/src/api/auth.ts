import { reactive } from 'vue'
import { api } from './http'

export interface AuthUser {
  id: number
  username: string
  display_name: string
  role: 'admin' | 'member'
  active: boolean
  /** 头像：data URL（128×128 webp/png），空串表示用姓名首字 */
  avatar: string
  created_at: string
  last_login_at: string
}

export interface AuthStatus {
  initialized: boolean
  user: AuthUser | null
}

/**
 * 全局登录态。
 *
 * 只放在内存里（不放 localStorage）：真正的凭据是 HttpOnly cookie，
 * 前端拿不到也不该拿。刷新页面重新问一次后端即可。
 */
export const authState = reactive<{
  loaded: boolean
  initialized: boolean
  user: AuthUser | null
}>({
  loaded: false,
  initialized: true,
  user: null,
})

export function isAdmin(): boolean {
  return authState.user?.role === 'admin'
}

/** 首次加载 / 强制刷新登录态。router 守卫和 App 挂载都会调它。 */
export async function ensureAuthLoaded(force = false): Promise<void> {
  if (authState.loaded && !force) return
  try {
    const { data } = await api.get<AuthStatus>('/auth/status', { timeout: 10000 })
    authState.initialized = data.initialized
    authState.user = data.user
  } catch {
    // 后端没起来时不要把用户顶到登录页——这属于可自愈的瞬时故障
    authState.initialized = authState.initialized ?? true
    authState.user = null
  } finally {
    authState.loaded = true
  }
}

export function setAuthUser(user: AuthUser | null): void {
  authState.user = user
  authState.loaded = true
  if (user) authState.initialized = true
}

export async function login(username: string, password: string): Promise<AuthUser> {
  const { data } = await api.post<{ ok: boolean; user: AuthUser }>(
    '/auth/login',
    { username, password },
    { timeout: 20000 },
  )
  setAuthUser(data.user)
  return data.user
}

/** 首次设置管理员：建完即登录。 */
export async function setupAdmin(payload: {
  username: string
  password: string
  display_name: string
}): Promise<AuthUser> {
  const { data } = await api.post<{ ok: boolean; user: AuthUser }>(
    '/auth/setup',
    payload,
    { timeout: 20000 },
  )
  setAuthUser(data.user)
  return data.user
}

export async function logout(): Promise<void> {
  try {
    await api.post('/auth/logout', {}, { timeout: 10000 })
  } finally {
    setAuthUser(null)
  }
}

export async function changeOwnPassword(oldPassword: string, newPassword: string): Promise<string> {
  const { data } = await api.post<{ ok: boolean; detail: string }>(
    '/auth/password',
    { old_password: oldPassword, new_password: newPassword },
    { timeout: 20000 },
  )
  // 改完自己的会话被后端清掉了，前端也要退出
  setAuthUser(null)
  return data.detail || '密码已修改'
}

/** 改自己的显示名称 / 头像（头像传空串表示清空，回退到姓名首字）。 */
export async function updateOwnProfile(payload: {
  display_name?: string
  avatar?: string
}): Promise<AuthUser> {
  const { data } = await api.patch<{ ok: boolean; user: AuthUser }>(
    '/auth/profile',
    payload,
    { timeout: 20000 },
  )
  setAuthUser(data.user)
  return data.user
}

export async function listUsers(): Promise<AuthUser[]> {
  const { data } = await api.get<{ users: AuthUser[] }>('/auth/users', { timeout: 15000 })
  return data.users || []
}

export async function createUser(payload: {
  username: string
  password: string
  display_name: string
  role: 'admin' | 'member'
  avatar?: string
}): Promise<AuthUser> {
  const { data } = await api.post<{ ok: boolean; user: AuthUser }>(
    '/auth/users',
    payload,
    { timeout: 20000 },
  )
  return data.user
}

export async function updateUser(
  id: number,
  payload: { display_name?: string; role?: 'admin' | 'member'; active?: boolean; avatar?: string },
): Promise<AuthUser> {
  const { data } = await api.patch<{ ok: boolean; user: AuthUser }>(
    `/auth/users/${id}`,
    payload,
    { timeout: 15000 },
  )
  return data.user
}

export async function resetUserPassword(id: number, newPassword: string): Promise<string> {
  const { data } = await api.post<{ ok: boolean; detail: string }>(
    `/auth/users/${id}/password`,
    { new_password: newPassword },
    { timeout: 20000 },
  )
  return data.detail || '密码已重置'
}

export async function deleteUser(id: number): Promise<void> {
  await api.delete(`/auth/users/${id}`, { timeout: 15000 })
}
