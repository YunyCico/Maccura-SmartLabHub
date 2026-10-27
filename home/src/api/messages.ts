import { api } from './http'

export type MessageSourceKey = 'dingtalk' | 'salesforce' | 'oa'
export type MessagePriority = '高' | '中' | '低'
export type MessageTone = 'blue' | 'orange' | 'green'

export interface MessageItem {
  source: MessageSourceKey
  external_id: string
  title: string
  detail: string
  due: string
  priority: MessagePriority
  url: string
  category: string
  /** 消息类型键：todo / notify / ding / calendar（仅钉钉有） */
  kind: string
  creator: string
  live: boolean
  /** 后端归一化后的展示字段 */
  source_label: string
  tone: MessageTone
  /** 后端附加的结构化字段（如钉钉行程的创建人、日历名） */
  extra?: Record<string, any>
}

export interface MessageSourceState {
  source: MessageSourceKey
  enabled: boolean
  count: number
  error: string
  reason?: string
  /** 钉钉启用的消息类型（todo/notify/ding/calendar） */
  kinds?: string[]
  /** 钉钉逐类型说明：未开放收件箱接口 / 缺权限 / 调用失败 */
  kind_notes?: Record<string, string>
}

export interface MessageFeed {
  items: MessageItem[]
  sources: MessageSourceState[]
  /** 全部来源未配置时为 true，首页据此显示"示例数据"提示 */
  using_placeholder: boolean
  /** 后端本次取数的时刻（UTC ISO），首页用它显示"上次更新 hh:mm:ss" */
  fetched_at?: string
  /** 本次是否命中了后端快照缓存（true 表示没有真打一遍上游接口） */
  cached?: boolean
}

export async function fetchMessages(userId = '', limit = 50): Promise<MessageFeed> {
  const response = await api.get<MessageFeed>('/messages', {
    params: { user_id: userId || undefined, limit },
    timeout: 20000,
  })
  return response.data
}

/**
 * 用手机号换钉钉 userid（无开发者权限、拿不到 CorpId 时的替代路径）。
 *
 * 凭据走请求体：用户可能刚在设置页填了 Secret 但还没保存，
 * 传过去就能直接查，不用先点保存。后端未收到时才回落到已保存的配置。
 */
export async function resolveDingTalkUserId(
  mobile: string,
  credentials: { app_key?: string; app_secret?: string; corp_id?: string } = {},
): Promise<{ ok: boolean; user_id?: string; detail?: string }> {
  const response = await api.post<{ ok: boolean; user_id?: string; detail?: string }>(
    '/messages/dingtalk/userid',
    { mobile, ...credentials },
    { timeout: 20000 },
  )
  return response.data
}
