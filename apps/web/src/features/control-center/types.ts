export type AsyncStatus = 'idle' | 'loading' | 'success' | 'error'

export type ConnectorStatus =
  | 'not_configured'
  | 'connecting'
  | 'ready'
  | 'permission_denied'
  | 'offline'
  | 'disabled'
  | 'scanning'
  | 'failed'

export type ConnectorKind = 'host' | 'pbix' | 'bi_activity' | 'directory'

export type Connector = {
  id: string
  name: string
  kind: ConnectorKind
  status: ConnectorStatus
  machine_name?: string
  path?: string
  purpose?: string
  read_only: boolean
  permission?: 'read_only' | 'application_read_only' | 'insufficient' | 'unknown'
  last_seen_at?: string
  last_scan_at?: string
  last_scan_status?: 'success' | 'failed' | 'partial' | 'never'
  discovered_count?: number
  offline_count?: number
  message?: string
}

export type OperationStatus =
  | 'queued'
  | 'waiting_for_worker'
  | 'scanning'
  | 'reading'
  | 'processing'
  | 'waiting_for_review'
  | 'resuming'
  | 'completed'
  | 'failed'

export type OperationEvent = {
  id: string
  operation_id: string
  stage: string
  status: OperationStatus
  message: string
  occurred_at: string
}

export type Operation = {
  id: string
  title: string
  status: OperationStatus
  stage: string
  progress_percent?: number
  started_at?: string
  updated_at: string
  completed_at?: string
  worker_name?: string
  worker_online: boolean
  discovered_count?: number
  processed_count?: number
  waiting_review_count?: number
  failed_count?: number
  blocking_reason?: string
  timeline: OperationEvent[]
}

export type ResultFact = {
  label: string
  value: string
  note?: string
}

export type LatestResult = {
  id: string
  title: string
  status: 'certified' | 'preclosed' | 'needs_attention'
  completed_at: string
  facts: ResultFact[]
}

export type PrimaryAction = {
  kind: 'review' | 'operation' | 'connectors' | 'none'
  label: string
  target: string
}

export type ControlOverview = {
  connector: {
    status: ConnectorStatus
    machine_name: string
    last_seen_at?: string
    message?: string
  }
  current_operation: Operation | null
  pending_review_count: number
  latest_result: LatestResult | null
  primary_action: PrimaryAction
  updated_at: string
}

export type OperationsResponse = {
  items: Operation[]
  updated_at: string
}

export type ProgressConnectionState = 'connecting' | 'live' | 'polling' | 'offline'

export type ReviewRisk = 'normal' | 'high'

export type ReviewEvidence = {
  label: string
  value: string
  source_reference?: string
}

export type ReviewSuggestion = {
  code: string
  label: string
  explanation: string
  recommended: boolean
}

export type ReviewItem = {
  id: string
  title: string
  summary: string
  status: 'pending' | 'claimed' | 'decided' | 'rejected'
  kind: string
  risk: ReviewRisk
  created_at: string
  operation_id?: string
  claimed_by_me: boolean
  claimed_by_name?: string
  evidence: ReviewEvidence[]
  suggestions: ReviewSuggestion[]
}

export type ReviewItemsResponse = {
  items: ReviewItem[]
}

export type ReviewDecisionInput = {
  decision: 'confirm' | 'reject'
  suggestion_code?: string
  note?: string
}

export type LlmMode = 'disabled' | 'cloud' | 'local'
export type LlmValidationStatus = 'not_configured' | 'pending' | 'available' | 'unavailable'

export type LlmTaskBinding = {
  task: string
  label: string
  model: string
  enabled: boolean
}

export type LlmConfiguration = {
  mode: LlmMode
  provider: string
  api_base: string
  default_model: string
  secret_configured: boolean
  validation_status: LlmValidationStatus
  validation_message?: string
  validated_at?: string
  task_bindings: LlmTaskBinding[]
}

export type LlmConfigurationInput = {
  mode: LlmMode
  provider: string
  api_base: string
  default_model: string
  api_key?: string
  task_bindings: LlmTaskBinding[]
}

export type MoreLink = {
  label: string
  description: string
  to: string
  permission?: 'manage_connectors' | 'view_results' | 'manage_llm' | 'manage_users' | 'view_system'
}
