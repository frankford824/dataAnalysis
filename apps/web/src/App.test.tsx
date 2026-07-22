import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'

const viewer = { id: 'u1', name: '林经理', email: 'lin@example.com', role: 'viewer', enterprise_id: 'e1', enterprise_name: '海风户外用品', store_ids: ['s1'] }
const implementer = { ...viewer, role: 'implementer', name: '王实施' }
const admin = { ...viewer, role: 'admin', name: '张管理员' }

function json(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function baseApi(user = viewer) {
  return vi.fn((input: RequestInfo | URL) => {
    const path = String(input)
    if (path.endsWith('/setup')) return json({ initialized: true })
    if (path.endsWith('/auth/me')) return json(user)
    if (path.endsWith('/stores')) return json([{ id: 's1', name: '北辰旗舰店', status: 'active' }])
    if (path.endsWith('/sources')) return json([{ id: 'src1', name: '销售明细', status: 'active' }])
    if (path.endsWith('/ingestions')) return json([])
    if (path.includes('/analytics/overview')) return json({ revenue: 0, refund: 0, fees: 0, profit: 0, trend: [], stores: [] })
    return json({ detail: `unmocked ${path}` }, 404)
  })
}

beforeEach(() => vi.restoreAllMocks())

describe('golden path application', () => {
  it('shows a real login when no session exists', async () => {
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => String(input).endsWith('/setup') ? json({ initialized: true }) : json({ detail: 'not authenticated' }, 401)))
    render(<MemoryRouter initialEntries={['/']}><App /></MemoryRouter>)
    expect(await screen.findByRole('heading', { name: '登录经营数据平台' })).toBeInTheDocument()
  })

  it('logs in without legacy identity headers', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path.endsWith('/setup')) return json({ initialized: true })
      if (path.endsWith('/auth/me')) return json({ detail: 'not authenticated' }, 401)
      if (path.endsWith('/auth/login')) return json({ user: admin, expires_at: '2026-07-22T00:00:00Z' })
      return baseApi(admin)(input)
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    render(<MemoryRouter initialEntries={['/login']}><App /></MemoryRouter>)
    await user.type(await screen.findByLabelText('邮箱'), 'admin@example.com')
    await user.type(screen.getByLabelText('密码'), 'safe-password')
    await user.click(screen.getByRole('button', { name: '登录' }))
    expect(await screen.findByRole('heading', { name: '首页' })).toBeInTheDocument()
    const loginCall = fetchMock.mock.calls.find(([url]) => String(url).endsWith('/auth/login'))
    expect(loginCall?.[1]?.credentials).toBe('include')
    const headers = loginCall?.[1]?.headers as Record<string, string>
    expect(headers['X-Enterprise-ID']).toBeUndefined()
    expect(headers['X-User-ID']).toBeUndefined()
    expect(headers['X-Role']).toBeUndefined()
  })

  it('hides upload and management from a restricted viewer', async () => {
    vi.stubGlobal('fetch', baseApi(viewer))
    render(<MemoryRouter initialEntries={['/']}><App /></MemoryRouter>)
    expect(await screen.findByRole('heading', { name: '首页' })).toBeInTheDocument()
    expect(screen.getAllByRole('link', { name: /经营看板/ }).length).toBeGreaterThan(0)
    expect(screen.queryByRole('link', { name: /添加本月数据/ })).not.toBeInTheDocument()
    expect(screen.queryByText('管理')).not.toBeInTheDocument()
  })

  it('uploads, confirms, and publishes through real endpoints', async () => {
    const run = { id: 'run1', original_filename: 'sales.csv', source_definition_id: 'src1', status: 'needs_confirmation', summary: { row_count: 2, revenue: 200 }, quality_result: { completeness: true } }
    const fetchMock = baseApi(implementer)
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path.endsWith('/ingestions/upload') && init?.method === 'POST') return json(run)
      if (path.endsWith('/ingestions/run1/confirm')) return json({ ...run, status: 'confirmed' })
      if (path.endsWith('/ingestions/run1/publish')) return json({ ...run, status: 'published' })
      if (path.endsWith('/setup')) return json({ initialized: true })
      if (path.endsWith('/auth/me')) return json(implementer)
      if (path.endsWith('/stores')) return json([{ id: 's1', name: '北辰旗舰店' }])
      if (path.endsWith('/sources')) return json([{ id: 'src1', name: '销售明细' }])
      return json({ detail: 'unmocked' }, 404)
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    render(<MemoryRouter initialEntries={['/data']}><App /></MemoryRouter>)
    await user.click(await screen.findByRole('button', { name: '继续添加文件' }))
    const file = new File(['order_id,revenue\n1,200'], 'sales.csv', { type: 'text/csv' })
    await user.upload(screen.getByLabelText('选择经营文件'), file)
    await user.click(screen.getByRole('button', { name: '上传并检查' }))
    expect(await screen.findByRole('heading', { name: '核对并更新' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '确认并更新看板' }))
    expect(await screen.findByRole('heading', { name: '经营看板已经更新' })).toBeInTheDocument()
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith('/ingestions/run1/publish'))).toBe(true))
  })
})
