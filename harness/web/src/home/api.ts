/** One-screen home: four bars, plain language only. */

export type HomeBar = {
  id: string
  title: string
  active: boolean
  summary: string
  action?: string | null
  tone?: string
}

export type HomeNotification = {
  id: string
  title: string
  body: string
}

export type HomeScope = {
  periodId: string
  runId: string
  storeId: string | null
  periodStart: string | null
  periodEnd: string | null
}

export type HomeResponse = {
  bars: HomeBar[]
  notifications: HomeNotification[]
  scope: HomeScope | null
}

const API_BASE = import.meta.env.VITE_API_BASE || '/api/v1'

export async function fetchHome(): Promise<HomeResponse> {
  const response = await fetch(`${API_BASE}/home`, { credentials: 'include' })
  if (!response.ok) {
    throw new Error(`主页加载失败 (${response.status})`)
  }
  return response.json() as Promise<HomeResponse>
}
