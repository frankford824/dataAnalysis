import type { Issue, Resource, SessionUser } from '../types'
import { request } from './http'

export const getDiagnostics = () => request<{ status: string; checks: Record<string, string>; ai_required: boolean }>('/health/diagnostics')
export const listProblems = () => request<Issue[]>('/issues')
export const listUsers = () => request<SessionUser[]>('/users')

export function updateIssue(id: string, body: { action: 'resolve' | 'retry_with_mapping' | 'reject'; resolution: string; source_definition_id?: string; field_mapping?: Record<string, string> }) {
  return request<Issue>(`/issues/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  })
}

export function inviteUser(body: { name: string; email: string; role: string; store_ids: string[] }) {
  return request<{ user: SessionUser; temporary_password: string }>('/users/invite', { method: 'POST', body: JSON.stringify(body) })
}

export function updateUser(id: string, body: { role?: string; store_ids?: string[]; status?: string }) {
  return request<SessionUser>(`/users/${id}`, { method: 'PATCH', body: JSON.stringify(body) })
}

export function getEmbedToken(id: string) {
  return request<{ token: string; embedded_id: string; expires_in: number }>(`/dashboards/${id}/embed-token`, { method: 'POST' })
}

export const asDashboard = (resource: Resource) => resource
