import { request } from '../../api/http'
import type {
  Connector,
  ControlOverview,
  LlmConfiguration,
  LlmConfigurationInput,
  LlmTaskBinding,
  Operation,
  OperationEvent,
  OperationStatus,
  OperationsResponse,
  ReviewDecisionInput,
  ReviewEvidence,
  ReviewItem,
  ReviewSuggestion,
} from './types'

const API_BASE = import.meta.env.VITE_API_BASE || '/api/v1'
const TASK_LABELS: Record<string, string> = {
  file_classification: '文件结构归类',
  rule_proposal: '字段语义建议',
  difference_explanation: '差异归因建议',
}

type JsonRecord = Record<string, unknown>

function record(value: unknown): JsonRecord {
  return value && typeof value === 'object' ? value as JsonRecord : {}
}

function text(value: unknown, fallback = '') {
  return typeof value === 'string' ? value : fallback
}

function count(value: unknown) {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

function operationStatus(raw: string): OperationStatus {
  return ({
    queued: 'queued',
    leased: 'waiting_for_worker',
    running: 'processing',
    waiting_review: 'waiting_for_review',
    succeeded: 'completed',
    completed: 'completed',
    failed: 'failed',
    cancelled: 'failed',
  } as Record<string, OperationStatus>)[raw] || 'queued'
}

function stageLabel(stage: string) {
  return ({
    queued: '等待外置执行器',
    claimed: '已由外置执行器领取',
    scanning: '发现文件',
    materializing: '只读获取文件',
    profiling: '识别文件结构',
    recomputing: '确定性核对',
    uploading: '提交处理结果',
    completed: '处理完成',
    failed: '处理失败',
  } as Record<string, string>)[stage] || stage || '等待开始'
}

export function normalizeOperationEvent(value: unknown): OperationEvent {
  const raw = record(value)
  const stage = text(raw.stage, text(raw.event_type, 'progress'))
  const eventType = text(raw.event_type)
  const status = operationStatus(
    eventType === 'completed' ? 'succeeded'
      : eventType === 'failed' ? 'failed'
        : eventType === 'waiting_review' ? 'waiting_review'
          : 'running',
  )
  return {
    id: text(raw.id, `${text(raw.job_id)}:${count(raw.sequence)}`),
    operation_id: text(raw.operation_id, text(raw.job_id)),
    stage: stageLabel(stage),
    status,
    message: text(raw.message, '处理状态已更新'),
    occurred_at: text(raw.occurred_at, text(raw.created_at, new Date().toISOString())),
  }
}

function operationTitle(jobType: string) {
  if (jobType === 'scan' || jobType === 'source_scan') return '读取 finance-win 数据来源'
  if (jobType === 'profile') return '识别文件结构'
  if (jobType === 'recompute') return '执行确定性核对'
  return '处理数据'
}

function normalizeOperation(value: unknown, events: OperationEvent[] = []): Operation {
  const raw = record(value)
  const result = record(raw.result)
  const resultBody = record(result.result)
  const rawStatus = text(raw.status, text(raw.state, 'queued'))
  const status = operationStatus(rawStatus)
  const updatedAt = text(raw.finished_at, text(raw.started_at, text(raw.created_at, new Date().toISOString())))
  const files = Array.isArray(resultBody.files) ? resultBody.files : []
  const discovered = count(resultBody.count) || files.length
  return {
    id: text(raw.id),
    title: operationTitle(text(raw.job_type)),
    status,
    stage: stageLabel(text(raw.stage, rawStatus)),
    progress_percent: count(raw.progress),
    started_at: text(raw.started_at) || undefined,
    updated_at: updatedAt,
    completed_at: text(raw.finished_at) || undefined,
    worker_name: text(raw.worker_name, raw.agent_id ? 'finance-win' : ''),
    worker_online: Boolean(raw.agent_id) && !['queued', 'failed', 'cancelled'].includes(rawStatus),
    discovered_count: discovered,
    processed_count: status === 'completed' ? discovered : count(raw.processed_count),
    waiting_review_count: count(raw.waiting_review_count),
    failed_count: status === 'failed' ? 1 : count(raw.failed_count),
    blocking_reason: status === 'waiting_for_worker' ? '等待 finance-win 外置执行器领取任务' : text(raw.blocking_reason) || undefined,
    timeline: events,
  }
}

async function operationEvents(id: string) {
  const values = await request<unknown[]>(`/operations/${encodeURIComponent(id)}/events`)
  return values.map(normalizeOperationEvent)
}

function normalizeConnector(value: unknown): Connector {
  const raw = record(value)
  const config = record(raw.config || raw.read_policy)
  const connectorType = text(raw.connector_type)
  const purpose = text(raw.purpose, text(config.purpose))
  const kind: Connector['kind'] =
    connectorType === 'pbix_inventory' ? 'pbix'
      : connectorType === 'bi_activity' ? 'bi_activity'
        : purpose === 'host' ? 'host'
          : 'directory'
  const active = text(raw.status) !== 'disabled' && raw.enabled !== false
  const permission = config.os_read_only === true ? 'read_only' : 'application_read_only'
  return {
    id: text(raw.id),
    name: text(raw.name, text(raw.logical_key, '未命名数据来源')),
    kind,
    status: active ? 'ready' : 'disabled',
    machine_name: text(raw.machine_name, 'finance-win'),
    path: text(raw.root_path, text(config.root_path)) || undefined,
    purpose,
    read_only: true,
    permission,
    last_scan_at: text(raw.last_scan_at) || undefined,
    last_scan_status: raw.last_scan_at ? 'success' : 'never',
    discovered_count: count(raw.discovered_count),
    offline_count: count(raw.offline_count),
    message: text(raw.message) || (permission === 'application_read_only' ? '程序只读；系统权限尚未强制只读' : undefined),
  }
}

function normalizeReview(value: unknown): ReviewItem {
  const raw = record(value)
  const context = record(raw.context_payload || raw.context || raw.payload || raw.disposition)
  const evidence = (Array.isArray(context.evidence) ? context.evidence : []).map((item): ReviewEvidence => {
    const entry = record(item)
    return {
      label: text(entry.label, '证据'),
      value: text(entry.value),
      source_reference: text(entry.source_reference) || undefined,
    }
  })
  const suggestions = (Array.isArray(context.suggestions) ? context.suggestions : []).map((item): ReviewSuggestion => {
    const entry = record(item)
    return {
      code: text(entry.code),
      label: text(entry.label, '建议处理'),
      explanation: text(entry.explanation),
      recommended: entry.recommended === true,
    }
  })
  const status = text(raw.status, 'pending')
  return {
    id: text(raw.id),
    title: text(context.title, text(raw.subject_type) === 'recon_difference' ? '核对差异需要确认' : '需要确认处理方式'),
    summary: text(context.summary, '请核对证据和影响范围后再处理。'),
    status: status === 'decided' ? 'decided' : status === 'claimed' ? 'claimed' : status === 'cancelled' ? 'rejected' : 'pending',
    kind: text(raw.subject_type, 'review'),
    risk: ['high', 'critical'].includes(text(raw.risk_level)) ? 'high' : 'normal',
    created_at: text(raw.created_at, new Date().toISOString()),
    operation_id: text(context.operation_id) || undefined,
    claimed_by_me: raw.claimed_by_me === true,
    claimed_by_name: text(raw.claimed_by_name) || undefined,
    evidence: evidence.length > 0 ? evidence : [{ label: '对象编号', value: text(raw.subject_id) }],
    suggestions: suggestions.length > 0 ? suggestions : [{
      code: 'acknowledge',
      label: '按当前证据继续',
      explanation: '仅处理本次事项，不会自动发布为通用规则。',
      recommended: true,
    }],
  }
}

function defaultTaskBindings(model = ''): LlmTaskBinding[] {
  return Object.entries(TASK_LABELS).map(([task, label]) => ({ task, label, model, enabled: false }))
}

function normalizeLlm(value: unknown, validation?: JsonRecord): LlmConfiguration {
  const raw = record(value)
  const provider = record(raw.provider)
  const models = Array.isArray(raw.models) ? raw.models.map(record) : []
  const policies = Array.isArray(raw.task_policies) ? raw.task_policies.map(record) : []
  const defaultModel = text(models[0]?.model_name, text(models[0]?.name))
  const policyByTask = new Map(policies.map((policy) => [text(policy.task), policy]))
  const validationStatus = text(validation?.status)
  const status: LlmConfiguration['validation_status'] =
    validationStatus === 'available' ? 'available'
      : ['unavailable', 'invalid'].includes(validationStatus) ? 'unavailable'
        : 'not_configured'
  return {
    mode: (text(provider.mode, 'disabled') as LlmConfiguration['mode']),
    provider: text(provider.name),
    api_base: text(provider.api_base),
    default_model: defaultModel,
    secret_configured: provider.has_api_key === true,
    validation_status: status,
    validation_message: text(validation?.message) || undefined,
    task_bindings: defaultTaskBindings(defaultModel).map((binding) => {
      const policy = policyByTask.get(binding.task)
      return policy ? {
        ...binding,
        model: text(policy.primary_model, defaultModel),
        enabled: policy.enabled === true,
      } : binding
    }),
  }
}

export async function getControlOverview(): Promise<ControlOverview> {
  const raw = record(await request<unknown>('/control/overview'))
  const agents = Array.isArray(raw.agents) ? raw.agents.map(record) : []
  const agent = agents[0]
  const rawOperation = raw.current_operation
  const operation = rawOperation ? normalizeOperation(rawOperation) : null
  const pending = count(raw.pending_review_count)
  return {
    connector: {
      status: !agent ? 'not_configured' : text(agent.status) === 'online' ? 'ready' : 'offline',
      machine_name: text(agent?.name, 'finance-win'),
      last_seen_at: text(agent?.last_heartbeat_at) || undefined,
      message: !agent ? '尚未注册 finance-win 外置执行器' : text(agent.status) !== 'online' ? '外置执行器心跳已中断' : undefined,
    },
    current_operation: operation,
    pending_review_count: pending,
    latest_result: null,
    primary_action: pending > 0
      ? { kind: 'review', label: '处理待确认事项', target: '/control/inbox' }
      : operation
        ? { kind: 'operation', label: '查看当前任务', target: '/control/operations' }
        : { kind: 'connectors', label: '查看数据来源', target: '/control/data-sources' },
    updated_at: text(raw.generated_at, new Date().toISOString()),
  }
}

export async function listConnectors() {
  const values = await request<unknown[]>('/connectors')
  return values.map(normalizeConnector)
}

export async function listOperations(): Promise<OperationsResponse> {
  const values = await request<unknown[]>('/operations')
  const timelines = await Promise.all(values.map((value) => operationEvents(text(record(value).id))))
  const items = values.map((value, index) => normalizeOperation(value, timelines[index]))
  const updatedAt = items.reduce((latest, item) => item.updated_at > latest ? item.updated_at : latest, '')
  return { items, updated_at: updatedAt || new Date().toISOString() }
}

export async function listReviewItems() {
  const values = await request<unknown[]>('/review-items?status=open')
  return { items: values.map(normalizeReview) }
}

export async function claimReviewItem(id: string) {
  return normalizeReview(await request<unknown>(`/review-items/${encodeURIComponent(id)}/claim`, { method: 'POST' }))
}

export async function decideReviewItem(id: string, input: ReviewDecisionInput) {
  const response = record(await request<unknown>(`/review-items/${encodeURIComponent(id)}/decide`, {
    method: 'POST',
    body: JSON.stringify({
      action: input.decision,
      note: input.note?.trim() || (input.decision === 'confirm' ? '确认系统建议并继续本次处理。' : '拒绝系统建议。'),
      payload: input.suggestion_code ? { suggestion_code: input.suggestion_code } : {},
    }),
  }))
  return normalizeReview(response.item || response)
}

export async function getLlmConfiguration() {
  return normalizeLlm(await request<unknown>('/llm/configuration'))
}

export async function saveLlmConfiguration(input: LlmConfigurationInput) {
  const models = input.default_model ? [{
    name: 'primary',
    model_name: input.default_model,
    timeout_seconds: 30,
    max_retries: 1,
    budget_cents: null,
  }] : []
  const response = await request<unknown>('/llm/configuration', {
    method: 'PUT',
    body: JSON.stringify({
      provider: {
        name: input.provider || (input.mode === 'disabled' ? 'disabled' : 'LiteLLM'),
        mode: input.mode,
        api_base: input.api_base || null,
        ...(input.api_key ? { api_key: input.api_key } : {}),
      },
      models,
      task_policies: input.task_bindings.map((binding) => ({
        task: binding.task,
        primary_model: binding.enabled && input.default_model ? 'primary' : null,
        fallback_model: null,
        enabled: binding.enabled,
        redaction_policy: { samples: 'masked', max_rows: 5 },
      })),
    }),
  })
  return normalizeLlm(response)
}

export async function validateLlmConfiguration() {
  const [configuration, validation] = await Promise.all([
    request<unknown>('/llm/configuration'),
    request<unknown>('/llm/configuration/validate', { method: 'POST' }),
  ])
  return normalizeLlm(configuration, record(validation))
}

export function operationStreamUrl() {
  return `${API_BASE}/operations/stream`
}
