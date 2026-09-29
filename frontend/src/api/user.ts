import { api } from './http'

export interface CurrentUser {
  user_id: string
  name: string
  role: string
  data_scope: string
  avatar: string
}

export interface DingTalkConfig {
  corp_id: string
  enabled: boolean
}

export async function fetchDingTalkConfig(): Promise<DingTalkConfig> {
  const response = await api.get<DingTalkConfig>('/dingtalk/config')
  return response.data
}

export async function fetchCurrentUser(code?: string | null): Promise<CurrentUser> {
  const response = await api.get<CurrentUser>('/me', { params: code ? { code } : undefined })
  return response.data
}
