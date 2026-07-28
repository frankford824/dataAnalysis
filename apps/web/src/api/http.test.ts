import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, request } from './http'

beforeEach(() => { vi.restoreAllMocks() })

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
    expect(init.headers).not.toHaveProperty('Authorization')
  })

  it('never exposes a bearer token to frontend JavaScript', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)
    await request('/stores')
    expect(fetchMock.mock.calls[0][1].headers.Authorization).toBeUndefined()
  })

  it('preserves structured recognition conflicts', async () => {
    const detail = { code: 'ambiguous_source', message: '请选择数据内容', options: [{ id: 's1', label: '销售明细' }] }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail }), { status: 409, headers: { 'Content-Type': 'application/json' } })))
    await expect(request('/ingestions/upload', { method: 'POST' })).rejects.toMatchObject({ status: 409, details: { detail } } satisfies Partial<ApiError>)
  })
})
