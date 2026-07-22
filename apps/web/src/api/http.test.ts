import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, request, setAccessToken } from './http'

beforeEach(() => { vi.restoreAllMocks(); setAccessToken(null) })

describe('authenticated HTTP client', () => {
  it('uses HttpOnly-compatible credentials without spoofed identity headers', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)
    await request('/auth/me')
    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect(init.credentials).toBe('include')
    expect(init.headers).not.toHaveProperty('X-Enterprise-ID')
    expect(init.headers).not.toHaveProperty('X-User-ID')
    expect(init.headers).not.toHaveProperty('X-Role')
  })

  it('sends an actual bearer token when the login endpoint returns one', async () => {
    setAccessToken('signed-token')
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)
    await request('/stores')
    expect(fetchMock.mock.calls[0][1].headers.Authorization).toBe('Bearer signed-token')
  })

  it('preserves structured recognition conflicts', async () => {
    const detail = { code: 'ambiguous_source', message: '请选择数据内容', options: [{ id: 's1', label: '销售明细' }] }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail }), { status: 409, headers: { 'Content-Type': 'application/json' } })))
    await expect(request('/ingestions/upload', { method: 'POST' })).rejects.toMatchObject({ status: 409, details: { detail } } satisfies Partial<ApiError>)
  })
})
