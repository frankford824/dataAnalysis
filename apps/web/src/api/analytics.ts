import type { CertifiedRow, DashboardSummary, FilterState } from '../types'
import { download, request } from './http'
import { listStores } from './resources'

export type Completeness = {
  period: string
  status: 'complete' | 'incomplete'
  expected_scope_count: number
  completed_scope_count: number
  missing: Array<{ source_logical_id: string; source_name: string; store_logical_id: string; store_name: string }>
}
type OverviewResponse = { metrics: Record<string, string | number>; completeness: Completeness }
type ExportRow = CertifiedRow & { cost?: number; product_cost?: number }

function params(filters: FilterState, exclusiveEnd = false) {
  const end = exclusiveEnd ? new Date(new Date(`${filters.dateTo}T00:00:00Z`).getTime() + 86_400_000).toISOString() : filters.dateTo
  const start = new Date(`${filters.dateFrom}T00:00:00Z`).toISOString()
  const query = new URLSearchParams({ date_from: start, date_to: end })
  if (filters.platformId) query.set('platform_id', filters.platformId)
  filters.storeIds.forEach((id) => query.append('store_id', id))
  return query
}

export async function getOverview(filters: FilterState): Promise<DashboardSummary> {
  const [overview, exported, stores] = await Promise.all([
    request<OverviewResponse>(`/analytics/overview?${params(filters, true)}`),
    request<{ rows: ExportRow[] }>(`/exports/certified?format=json&${params(filters, true)}`),
    listStores(),
  ])
  const names = new Map(stores.map((store) => [store.id, store.name]))
  const months = new Map<string, { revenue: number; profit: number }>()
  const storeRows = new Map<string, { order_count: number; revenue: number; refund: number; fees: number; cost: number; profit: number }>()
  for (const row of exported.rows) {
    const revenue = Number(row.revenue)
    const refund = Number(row.refund)
    const fees = Number(row.fees)
    const profit = Number(row.profit)
    const cost = Number(row.product_cost ?? row.cost ?? Math.max(0, revenue - refund - fees - profit))
    const month = row.period_start.slice(0, 10)
    const monthValue = months.get(month) || { revenue: 0, profit: 0 }
    monthValue.revenue += revenue
    monthValue.profit += profit
    months.set(month, monthValue)
    const storeId = row.store_id || 'enterprise'
    const current = storeRows.get(storeId) || { order_count: 0, revenue: 0, refund: 0, fees: 0, cost: 0, profit: 0 }
    current.order_count += Number(row.order_count || 0)
    current.revenue += revenue
    current.refund += refund
    current.fees += fees
    current.cost += cost
    current.profit += profit
    storeRows.set(storeId, current)
  }
  const metric = (name: string) => Number(overview.metrics[name] || 0)
  const totalCost = metric('cost') || metric('product_cost') || Array.from(storeRows.values()).reduce((sum, row) => sum + row.cost, 0)
  return {
    order_count: metric('order_count'), revenue: metric('revenue'), refund: metric('refund'), fees: metric('fees'), cost: totalCost, profit: metric('profit'),
    trend: Array.from(months, ([month, values]) => ({ month, ...values })),
    stores: Array.from(storeRows, ([id, values]) => ({ id, name: names.get(id) || '企业汇总', ...values, profit_margin: values.revenue ? values.profit / values.revenue * 100 : 0 })),
  }
}

export async function getCompleteness(filters: FilterState) {
  const overview = await request<OverviewResponse>(`/analytics/overview?${params(filters, true)}`)
  return overview.completeness
}

export function exportCertified(filters: FilterState, format: 'csv' | 'xlsx') {
  const query = params(filters, true)
  query.set('format', format)
  return download(`/exports/certified?${query}`, `经营数据-${filters.dateFrom}-${filters.dateTo}.${format}`)
}

export function askBusiness(question: string, filters: FilterState, type: string) {
  const start = new Date(`${filters.dateFrom}T00:00:00Z`).toISOString()
  const exclusiveEnd = new Date(new Date(`${filters.dateTo}T00:00:00Z`).getTime() + 86_400_000).toISOString()
  return request<{ answer: string; value?: string; metric?: string; options?: string[] }>('/business-questions', { method: 'POST', body: JSON.stringify({ question, question_type: type, platform_id: filters.platformId || null, store_ids: filters.storeIds, date_from: start, date_to: exclusiveEnd }) })
}
