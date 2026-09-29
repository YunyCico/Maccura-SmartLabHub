import { api } from './http'

export interface DashboardSummary {
  service_request_count: number
  completed_count: number
  average_satisfaction: string
  pending_import_count: number
  recent_tasks: Array<Record<string, unknown>>
}

export async function fetchDashboardSummary(): Promise<DashboardSummary> {
  const response = await api.get<DashboardSummary>('/dashboard/summary')
  return response.data
}
