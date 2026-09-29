import { api } from './http'

export interface ImportSheetPreview {
  name: string
  kind: 'table' | 'info'
  header_rows: string
  rows: number
  columns: string[]
  detected_fields: Record<string, string>
  warnings: string[]
  preview: Array<Record<string, string>>
}

export interface ImportPreviewResult {
  filename: string
  sheet_count: number
  total_rows: number
  max_columns: number
  sheets: ImportSheetPreview[]
}

interface ImportPreviewResponse {
  status: string
  data: ImportPreviewResult
}

export async function previewImport(file: File): Promise<ImportPreviewResult> {
  const body = new FormData()
  body.append('file', file)
  const response = await api.post<ImportPreviewResponse>('/import/preview', body, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
  return response.data.data
}

export interface ImportBatch {
  id: number
  original_filename: string
  stored_filename: string
  stored_path: string
  total_rows: number
  sheet_count: number
  imported_by: string
  created_at: string
}

export async function confirmImport(file: File): Promise<ImportBatch> {
  const body = new FormData()
  body.append('file', file)
  const response = await api.post<{ status: string; data: ImportBatch }>('/imports/confirm', body, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
  return response.data.data
}
