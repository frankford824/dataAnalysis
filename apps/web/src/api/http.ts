const API_BASE = import.meta.env.VITE_API_BASE || '/api/v1'
let accessToken: string | null = null

export class ApiError extends Error {
  constructor(public status: number, message: string, public details?: unknown) {
    super(message)
  }
}

export function setAccessToken(token: string | null) {
  accessToken = token
}

function errorMessage(body: unknown, fallback: string) {
  if (!body || typeof body !== 'object' || !('detail' in body)) return fallback
  const detail = (body as { detail: unknown }).detail
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object' && 'message' in detail) return String(detail.message)
  return fallback
}

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const isForm = init.body instanceof FormData
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    credentials: 'include',
    headers: {
      ...(isForm || !init.body ? {} : { 'Content-Type': 'application/json' }),
      ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
      ...init.headers,
    },
  })
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    if (response.status === 401) window.dispatchEvent(new Event('auth-expired'))
    throw new ApiError(response.status, errorMessage(body, `请求失败 (${response.status})`), body)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export async function download(path: string, fallbackName: string) {
  const response = await fetch(`${API_BASE}${path}`, {
    credentials: 'include',
    headers: accessToken ? { Authorization: `Bearer ${accessToken}` } : {},
  })
  if (!response.ok) throw new ApiError(response.status, `导出失败 (${response.status})`)
  const blob = await response.blob()
  const disposition = response.headers.get('Content-Disposition') || ''
  const match = /filename="?([^";]+)"?/i.exec(disposition)
  const anchor = document.createElement('a')
  anchor.href = URL.createObjectURL(blob)
  anchor.download = match?.[1] || fallbackName
  anchor.click()
  URL.revokeObjectURL(anchor.href)
}
