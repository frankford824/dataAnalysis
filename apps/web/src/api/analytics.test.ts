import { beforeEach, expect, it, vi } from 'vitest'
import { getOverview } from './analytics'

const json = (body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } }))

beforeEach(() => vi.restoreAllMocks())

it('combines certified rows with overview totals without legacy tenant headers', async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL, _init?: RequestInit) => {
    const path = String(input)
    if (path.includes('/analytics/overview')) return json({ metrics: { order_count: 6, revenue: 200, refund: 10, fees: 20, profit: 120 }, completeness: { period: '2026-07', status: 'complete', expected_scope_count: 2, completed_scope_count: 2, missing: [] } })
    if (path.includes('/exports/certified')) return json({ rows: [{ store_id: 's1', period_start: '2026-07-01', order_count: 6, revenue: 200, refund: 10, fees: 20, profit: 120, product_cost: 50 }] })
    if (path.endsWith('/stores')) return json([{ id: 's1', name: '测试店铺' }])
    return json({ detail: 'not found' })
  })
  vi.stubGlobal('fetch', fetchMock)

  const result = await getOverview({ platformId: '', storeIds: ['s1'], dateFrom: '2026-07-01', dateTo: '2026-07-31' })

  expect(result.cost).toBe(50)
  expect(result.order_count).toBe(6)
  expect(result.stores[0]).toMatchObject({ name: '测试店铺', order_count: 6, profit_margin: 60 })
  expect(fetchMock).toHaveBeenCalledTimes(3)
  expect(fetchMock.mock.calls.every(([, init]) => !(init?.headers as Record<string, string>)?.['X-Enterprise-ID'])).toBe(true)
})
