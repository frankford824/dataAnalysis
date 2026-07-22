export type Role = 'platform_admin' | 'admin' | 'implementer' | 'analyst' | 'viewer'

export type SessionUser = {
  id: string
  email: string
  name: string
  role: Role
  enterprise_id: string
  enterprise_name?: string
  store_ids?: string[]
  must_change_password?: boolean
  permissions?: string[]
}

export type Resource = {
  id: string
  name: string
  status?: string
  [key: string]: unknown
}

export type StoreResource = Resource & {
  platform_account_id?: string
  activation_at?: string
}

export type SourceResource = Resource & {
  arrival_frequency?: string
  file_types?: string[]
  required?: boolean
}

export type FilterState = {
  platformId: string
  storeIds: string[]
  dateFrom: string
  dateTo: string
}

export type CertifiedRow = {
  store_id: string | null
  period_start: string
  grain: string
  row_count: number
  order_count: number
  revenue: number
  refund: number
  fees: number
  cost?: number
  profit: number
}

export type IngestionRun = {
  id: string
  original_filename: string
  status: string
  source_definition_id: string
  store_id?: string
  source_sha256?: string
  coverage_start?: string
  coverage_end?: string
  quality_result?: Record<string, unknown>
  summary?: Record<string, unknown>
  deduplicated?: boolean
  created_at?: string
}

export type Issue = {
  id: string
  kind: string
  user_message: string
  technical_detail?: Record<string, unknown>
  status: string
  created_at: string
  ingestion_run_id?: string
}

export type DashboardSummary = {
  revenue: number
  refund: number
  fees: number
  cost: number
  profit: number
  trend: Array<{ month: string; revenue: number; profit: number }>
  stores: Array<{ id: string; name: string; revenue: number; refund: number; fees: number; cost: number; profit: number; profit_margin: number }>
}
