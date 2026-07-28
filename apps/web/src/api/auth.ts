import type { SessionUser } from '../types'
import { request } from './http'

type LoginResponse = { user: SessionUser; expires_at: string }

export async function login(email: string, password: string) {
  const result = await request<LoginResponse>('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ email, password }),
  })
  return result.user
}

export function getSession() {
  return request<SessionUser>('/auth/me')
}

export async function logout() {
  await request<{ logged_out: boolean }>('/auth/logout', { method: 'POST' })
}

export function changePassword(currentPassword: string, newPassword: string) {
  return request<{ changed: boolean }>('/auth/change-password', {
    method: 'POST',
    body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
  })
}

export function setupStatus() {
  return request<{ initialized: boolean }>('/setup')
}

export function completeSetup(body: { enterprise_name: string; platform: string; platform_account_name: string; store_name: string; activation_at: string; name: string; email: string; password: string }) {
  return request<LoginResponse>('/setup', { method: 'POST', body: JSON.stringify(body) })
}
