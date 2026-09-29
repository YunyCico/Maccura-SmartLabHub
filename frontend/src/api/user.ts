import { api } from './http'

export interface CurrentUser {
  user_id: string
  name: string
  role: string
  data_scope: string
}

export async function fetchCurrentUser(): Promise<CurrentUser> {
  const response = await api.get<CurrentUser>('/me')
  return response.data
}
