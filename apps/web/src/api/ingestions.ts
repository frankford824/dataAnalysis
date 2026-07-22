import type { IngestionRun } from '../types'
import { request } from './http'

export type RecognitionOption = { id: string; label: string }
export type RecognitionProblem = { code: string; message: string; options: RecognitionOption[] }

export function listIngestions() {
  return request<IngestionRun[]>('/ingestions')
}

export function getIngestion(id: string) {
  return request<IngestionRun>(`/ingestions/${id}`)
}

export function uploadFile(file: File, sourceId?: string, storeId?: string) {
  const body = new FormData()
  body.append('file', file)
  if (sourceId) body.append('source_definition_id', sourceId)
  if (storeId) body.append('store_id', storeId)
  body.append('backfill', 'false')
  return request<IngestionRun>('/ingestions/upload', { method: 'POST', body })
}

export function confirmIngestion(id: string, accepted: boolean, note?: string) {
  return request<IngestionRun>(`/ingestions/${id}/confirm`, {
    method: 'POST',
    body: JSON.stringify({ accepted, note }),
  })
}

export function publishIngestion(id: string) {
  return request<IngestionRun>(`/ingestions/${id}/publish`, { method: 'POST' })
}
