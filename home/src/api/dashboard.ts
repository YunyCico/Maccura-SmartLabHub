import { api } from './http'

export interface DashboardSummary {
  dataset_count: number
  sheet_count: number
  row_count: number
  field_count: number
  result_count: number
  recent_tasks: Array<Record<string, unknown>>
}

export async function fetchDashboardSummary(): Promise<DashboardSummary> {
  const response = await api.get<DashboardSummary>('/dashboard/summary')
  return response.data
}
