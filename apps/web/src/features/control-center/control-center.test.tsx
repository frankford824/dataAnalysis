import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import InboxPage from './InboxPage'
import LlmBindingsPage from './LlmBindingsPage'
import OperationsPage from './OperationsPage'
import WorkCenterPage from './WorkCenterPage'
import type { LlmConfiguration, Operation, ReviewItem } from './types'

function json(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

const operation: Operation = {
  id: 'op-1',
  title: '读取 7 月经营文件',
  status: 'processing',
  stage: '确定性处理',
  updated_at: '2026-07-22T08:10:00Z',
  worker_name: 'finance-win',
  worker_online: true,
  discovered_count: 12,
  processed_count: 8,
  waiting_review_count: 0,
  failed_count: 0,
  timeline: [{
    id: 'event-1',
    operation_id: 'op-1',
    stage: '发现文件',
    status: 'scanning',
    message: '发现 12 个符合读取范围的文件',
    occurred_at: '2026-07-22T08:00:00Z',
  }],
}

class FakeEventSource {
  static latest: FakeEventSource | null = null
  onopen: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onerror: ((event: Event) => void) | null = null
  close = vi.fn()

  constructor(public url: string) {
    FakeEventSource.latest = this
  }
}

beforeEach(() => {
  FakeEventSource.latest = null
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('control center progress', () => {
  it('falls back to polling when the SSE connection drops', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const path = String(input)
      if (path.endsWith('/operations/op-1/events')) return json([{
        id: 'event-1',
        job_id: 'op-1',
        sequence: 1,
        stage: 'scanning',
        event_type: 'progress',
        message: '发现 12 个符合读取范围的文件',
        occurred_at: '2026-07-22T08:00:00Z',
      }])
      if (path.endsWith('/operations')) return json([{
        id: 'op-1',
        job_type: 'scan',
        status: 'running',
        stage: 'recomputing',
        started_at: operation.updated_at,
        agent_id: 'agent-1',
        worker_name: 'finance-win',
        result: { result: { count: 12 } },
        processed_count: 8,
      }])
      return json({ detail: `unmocked ${path}` }, 404)
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('EventSource', FakeEventSource)

    render(<OperationsPage />)
    expect(await screen.findByRole('heading', { name: '读取 finance-win 数据来源' })).toBeInTheDocument()
    expect(FakeEventSource.latest?.url).toContain('/api/v1/operations/stream')

    act(() => FakeEventSource.latest?.onerror?.(new Event('error')))

    expect(await screen.findByText('实时连接中断，正在定时刷新')).toBeInTheDocument()
    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(2))
    expect(screen.getByText('发现 12 个符合读取范围的文件')).toBeInTheDocument()
  })

  it('shows the real agent-offline blocker without inventing progress', async () => {
    vi.stubGlobal('fetch', vi.fn(() => json({
      agents: [{
        id: 'agent-1',
        name: 'finance-win',
        status: 'offline',
        last_heartbeat_at: '2026-07-22T08:00:00Z',
      }],
      current_operation: {
        id: 'op-1',
        job_type: 'scan',
        status: 'leased',
        stage: 'claimed',
        created_at: '2026-07-22T08:00:00Z',
      },
      pending_review_count: 0,
      generated_at: '2026-07-22T08:10:00Z',
    })))

    render(<WorkCenterPage />)
    expect(await screen.findByText('finance-win 当前不可用')).toBeInTheDocument()
    expect(screen.getByText('外置执行器心跳已中断')).toBeInTheDocument()
    expect(screen.getByText('等待 finance-win 外置执行器领取任务')).toBeInTheDocument()
    expect(screen.queryByText(/%/)).not.toBeInTheDocument()
  })
})

describe('control center inbox', () => {
  it('claims and confirms a review item through the real action endpoints', async () => {
    const pending: ReviewItem = {
      id: 'review-1',
      title: '确认这份文件的用途',
      summary: '文件可能是平台账单，也可能是资金流水。',
      status: 'pending',
      kind: 'source_classification',
      risk: 'normal',
      created_at: '2026-07-22T08:00:00Z',
      claimed_by_me: false,
      evidence: [{ label: '文件', value: '7月账单.xlsx', source_reference: 'finance-win · 只读路径' }],
      suggestions: [{ code: 'platform_statement', label: '平台账单', explanation: '表头与平台账单模板一致', recommended: true }],
    }
    const claimed = { ...pending, status: 'claimed' as const, claimed_by_me: true, claimed_by_name: '当前用户' }
    const decided = { ...claimed, status: 'decided' as const }
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      const raw = {
        id: 'review-1',
        subject_type: 'source_classification',
        subject_id: '7月账单.xlsx',
        risk_level: 'normal',
        status: 'pending',
        created_at: pending.created_at,
        context_payload: {
          title: pending.title,
          summary: pending.summary,
          evidence: pending.evidence,
          suggestions: pending.suggestions,
        },
      }
      if (path.includes('/review-items?status=open')) return json([raw])
      if (path.endsWith('/review-items/review-1/claim') && init?.method === 'POST') {
        return json({ ...raw, status: claimed.status, claimed_by_me: true, claimed_by_name: claimed.claimed_by_name })
      }
      if (path.endsWith('/review-items/review-1/decide') && init?.method === 'POST') {
        return json({ item: { ...raw, status: decided.status, claimed_by_me: true } })
      }
      return json({ detail: 'unmocked' }, 404)
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    render(<InboxPage />)
    await user.click(await screen.findByRole('button', { name: /确认这份文件的用途/ }))
    await user.click(screen.getByRole('button', { name: '领取并处理' }))
    await user.click(await screen.findByRole('button', { name: '确认此处理' }))

    expect(await screen.findByText('目前没有待确认事项')).toBeInTheDocument()
    const decideCall = fetchMock.mock.calls.find(([url]) => String(url).endsWith('/review-items/review-1/decide'))
    expect(JSON.parse(String(decideCall?.[1]?.body))).toEqual({
      action: 'confirm',
      note: '确认系统建议并继续本次处理。',
      payload: { suggestion_code: 'platform_statement' },
    })
  })
})

describe('control center LLM configuration', () => {
  const disabled: LlmConfiguration = {
    mode: 'disabled',
    provider: '',
    api_base: '',
    default_model: '',
    secret_configured: false,
    validation_status: 'not_configured',
    task_bindings: [],
  }

  it('states that deterministic processing continues when LLM is disabled', async () => {
    vi.stubGlobal('fetch', vi.fn(() => json(disabled)))
    render(<LlmBindingsPage />)
    expect(await screen.findByText('当前不使用 LLM')).toBeInTheDocument()
    expect(screen.getByText('finance-win 自动发现、确定性规则、对账、收件箱和结果查看不受影响。')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '验证连接' })).not.toBeInTheDocument()
  })

  it('shows the server validation failure without exposing the configured secret', async () => {
    const cloud = {
      provider: {
        mode: 'cloud',
        name: 'OpenAI',
        api_base: 'https://api.example.test/v1',
        has_api_key: true,
      },
      models: [{ name: 'primary', model_name: 'model-a' }],
      task_policies: [{ task: 'file_classification', primary_model: 'model-a', enabled: true }],
    }
    const failed = { status: 'unavailable', message: '网关无法连接模型服务' }
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path.endsWith('/llm/configuration') && !init?.method) return json(cloud)
      if (path.endsWith('/llm/configuration/validate') && init?.method === 'POST') return json(failed)
      return json({ detail: 'unmocked' }, 404)
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    render(<LlmBindingsPage />)
    expect(await screen.findByText('密钥已配置，系统不会将原值返回浏览器。')).toBeInTheDocument()
    expect(screen.getByLabelText(/^API 密钥/)).toHaveValue('')
    await user.click(screen.getByRole('button', { name: '验证连接' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('网关无法连接模型服务')
    expect(screen.getByText('不可用')).toBeInTheDocument()
  })
})
