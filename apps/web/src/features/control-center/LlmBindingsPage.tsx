import { CheckCircle2, CircleAlert, Cloud, Cpu, Save, ShieldCheck, TestTube2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import { getLlmConfiguration, saveLlmConfiguration, validateLlmConfiguration } from './api'
import { ControlError, ControlLoading } from './components/ControlStates'
import { formatDateTime, PageIntro, StatusMark } from './components/ControlPrimitives'
import { useControlRequest } from './hooks/useControlRequest'
import type { LlmConfigurationInput, LlmMode } from './types'
import './control-center.css'

const EMPTY_FORM: LlmConfigurationInput = {
  mode: 'disabled',
  provider: '',
  api_base: '',
  default_model: '',
  task_bindings: [],
}

function validationTone(status: string) {
  if (status === 'available') return 'success' as const
  if (status === 'unavailable') return 'danger' as const
  if (status === 'pending') return 'info' as const
  return 'neutral' as const
}

function validationLabel(status: string) {
  if (status === 'available') return '可用'
  if (status === 'unavailable') return '不可用'
  if (status === 'pending') return '正在验证'
  return '尚未验证'
}

export default function LlmBindingsPage() {
  const state = useControlRequest(getLlmConfiguration)
  const [form, setForm] = useState<LlmConfigurationInput>(EMPTY_FORM)
  const [busy, setBusy] = useState<'save' | 'validate' | null>(null)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    if (!state.data) return
    setForm({
      mode: state.data.mode,
      provider: state.data.provider,
      api_base: state.data.api_base,
      default_model: state.data.default_model,
      task_bindings: state.data.task_bindings,
    })
  }, [state.data])

  const setMode = (mode: LlmMode) => setForm((current) => ({ ...current, mode }))
  const save = async () => {
    setBusy('save'); setError(''); setMessage('')
    try {
      const next = await saveLlmConfiguration(form)
      state.setData(next)
      setForm((current) => ({ ...current, api_key: undefined }))
      setMessage(next.mode === 'disabled' ? 'LLM 已禁用。确定性读取、核对和认证仍正常运行。' : '配置已保存，请执行连接验证。')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '保存失败')
    } finally { setBusy(null) }
  }
  const validate = async () => {
    setBusy('validate'); setError(''); setMessage('')
    try {
      const next = await validateLlmConfiguration()
      state.setData(next)
      if (next.validation_status === 'unavailable') setError(next.validation_message || '模型连接不可用')
      else setMessage('连接验证通过。')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '验证失败')
    } finally { setBusy(null) }
  }

  if (state.loading) return <ControlLoading label="正在读取 LLM 配置…" />
  if (state.error) return <ControlError message={state.error} onRetry={state.reload} />

  return <div className="cc-page">
    <PageIntro title="LLM 配置" description="LLM 只用于理解、归类和提出建议，不参与正式金额计算，也不能绕过确认与认证门禁。" />
    <form className="cc-llm-form" onSubmit={(event) => { event.preventDefault(); void save() }}>
      <fieldset className="cc-mode-picker">
        <legend>运行方式</legend>
        <label className={form.mode === 'disabled' ? 'is-selected' : ''}><input type="radio" name="llm-mode" checked={form.mode === 'disabled'} onChange={() => setMode('disabled')} /><ShieldCheck aria-hidden="true" /><span><strong>禁用</strong><small>完全使用确定性处理</small></span></label>
        <label className={form.mode === 'cloud' ? 'is-selected' : ''}><input type="radio" name="llm-mode" checked={form.mode === 'cloud'} onChange={() => setMode('cloud')} /><Cloud aria-hidden="true" /><span><strong>云模型</strong><small>通过统一网关连接</small></span></label>
        <label className={form.mode === 'local' ? 'is-selected' : ''}><input type="radio" name="llm-mode" checked={form.mode === 'local'} onChange={() => setMode('local')} /><Cpu aria-hidden="true" /><span><strong>本地模型</strong><small>连接客户内网模型服务</small></span></label>
      </fieldset>

      {form.mode === 'disabled' ? <div className="cc-llm-disabled"><ShieldCheck aria-hidden="true" /><div><strong>当前不使用 LLM</strong><p>finance-win 自动发现、确定性规则、对账、收件箱和结果查看不受影响。</p></div></div> : <div className="cc-llm-settings">
        <div className="cc-form-grid">
          <label className="cc-field">服务商<input value={form.provider} onChange={(event) => setForm((current) => ({ ...current, provider: event.target.value }))} required placeholder={form.mode === 'local' ? '例如：本地 LiteLLM' : '例如：OpenAI'} /></label>
          <label className="cc-field">API 地址<input type="url" value={form.api_base} onChange={(event) => setForm((current) => ({ ...current, api_base: event.target.value }))} required placeholder="https://…" /></label>
          <label className="cc-field">默认模型<input value={form.default_model} onChange={(event) => setForm((current) => ({ ...current, default_model: event.target.value }))} required /></label>
          <label className="cc-field">API 密钥
            <input type="password" value={form.api_key || ''} onChange={(event) => setForm((current) => ({ ...current, api_key: event.target.value || undefined }))} placeholder={state.data?.secret_configured ? '已配置；留空表示不更改' : '请输入密钥'} autoComplete="new-password" />
            <small>{state.data?.secret_configured ? '密钥已配置，系统不会将原值返回浏览器。' : '尚未配置密钥。'}</small>
          </label>
        </div>
        <fieldset className="cc-task-bindings">
          <legend>允许使用 LLM 的任务</legend>
          {form.task_bindings.map((binding, index) => <div key={binding.task}>
            <label><input type="checkbox" checked={binding.enabled} onChange={(event) => setForm((current) => ({ ...current, task_bindings: current.task_bindings.map((item, itemIndex) => itemIndex === index ? { ...item, enabled: event.target.checked } : item) }))} />{binding.label}</label>
            <label><span className="cc-sr-only">{binding.label}使用的模型</span><input value={binding.model} disabled={!binding.enabled} onChange={(event) => setForm((current) => ({ ...current, task_bindings: current.task_bindings.map((item, itemIndex) => itemIndex === index ? { ...item, model: event.target.value } : item) }))} /></label>
          </div>)}
        </fieldset>
      </div>}

      <div className="cc-validation-row">
        <div>{state.data?.validation_status === 'available' ? <CheckCircle2 aria-hidden="true" /> : <CircleAlert aria-hidden="true" />}<span><small>连接状态</small><strong><StatusMark tone={validationTone(state.data?.validation_status || 'not_configured')}>{validationLabel(state.data?.validation_status || 'not_configured')}</StatusMark></strong>{state.data?.validated_at ? <small>验证于 {formatDateTime(state.data.validated_at)}</small> : null}</span></div>
        {form.mode !== 'disabled' ? <button type="button" className="cc-button cc-button--secondary" onClick={() => void validate()} disabled={busy !== null}><TestTube2 aria-hidden="true" />{busy === 'validate' ? '正在验证…' : '验证连接'}</button> : null}
      </div>
      {message ? <p className="cc-form-success" role="status">{message}</p> : null}
      {error ? <p className="cc-form-error" role="alert"><CircleAlert aria-hidden="true" />{error}</p> : null}
      <div className="cc-form-actions"><button type="submit" className="cc-button cc-button--primary" disabled={busy !== null}><Save aria-hidden="true" />{busy === 'save' ? '正在保存…' : '保存配置'}</button></div>
    </form>
  </div>
}
