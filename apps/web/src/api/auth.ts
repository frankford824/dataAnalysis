import type { SessionUser } from '../types'
import { request, setAccessToken } from './http'

type LoginResponse = { user: SessionUser; access_token?: string; expires_at: string }

export async function login(email: string, password: string) {
  const result = await request<LoginResponse>('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ email, password }),
  })
  setAccessToken(result.access_token || null)
  return result.user
}

export function getSession() {
  return request<SessionUser>('/auth/me')
}

export async function logout() {
  try {
    await request<{ logged_out: boolean }>('/auth/logout', { method: 'POST' })
  } finally {
    setAccessToken(null)
  }
}

export function setupStatus() {
  return request<{ initialized: boolean }>('/setup')
}

export function completeSetup(body: { enterprise_name: string; platform: string; platform_account_name: string; store_name: string; activation_at: string; name: string; email: string; password: string }) {
  return request<LoginResponse>('/setup', { method: 'POST', body: JSON.stringify(body) })
}
