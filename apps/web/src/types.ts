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
  status?: string
}

export type Resource = {
  id: string
  name: string
  status?: string
  [key: string]: unknown
}

export type PlatformResource = Resource & {
  platform?: string
  external_account_id?: string
  logical_id?: string
}

export type StoreResource = Resource & {
  platform_account_id: string
  activation_at?: string
  external_store_id?: string
  logical_id?: string
}

export type SourceResource = Resource & {
  arrival_frequency?: string
  file_types?: string[]
  required?: boolean
  activation_at?: string
  coverage_time_field?: string
  data_granularity?: string
  import_mode?: 'monthly_snapshot' | 'incremental'
  source_kind?: 'orders' | 'fees' | 'mixed'
  field_aliases?: Record<string, string[]>
  dedupe_keys?: string[]
  amount_directions?: Record<string, 'positive' | 'negative'>
  validations?: Array<Record<string, unknown>>
  logical_id?: string
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
  order_count: number
  revenue: number
  refund: number
  fees: number
  cost: number
  profit: number
  trend: Array<{ month: string; revenue: number; profit: number }>
  stores: Array<{ id: string; name: string; order_count: number; revenue: number; refund: number; fees: number; cost: number; profit: number; profit_margin: number }>
}
