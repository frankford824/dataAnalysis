import { ArrowLeft, Check, CircleAlert, LockKeyhole, X } from 'lucide-react'
import { useEffect, useState } from 'react'
import { claimReviewItem, decideReviewItem, listReviewItems } from './api'
import { ControlEmpty, ControlError, ControlLoading } from './components/ControlStates'
import { formatDateTime, PageIntro, StatusMark } from './components/ControlPrimitives'
import { useControlRequest } from './hooks/useControlRequest'
import type { ReviewDecisionInput, ReviewItem } from './types'
import './control-center.css'

function replaceItem(items: ReviewItem[], next: ReviewItem) {
  if (next.status === 'decided' || next.status === 'rejected') return items.filter((item) => item.id !== next.id)
  return items.map((item) => item.id === next.id ? next : item)
}

export default function InboxPage() {
  const state = useControlRequest(listReviewItems)
  const items = state.data?.items || []
  const [selectedId, setSelectedId] = useState('')
  const [suggestionCode, setSuggestionCode] = useState('')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState('')
  const selected = items.find((item) => item.id === selectedId) || null

  useEffect(() => {
    if (selectedId && !items.some((item) => item.id === selectedId)) setSelectedId('')
  }, [items, selectedId])

  useEffect(() => {
    if (!selected) return
    setSuggestionCode(selected.suggestions.find((suggestion) => suggestion.recommended)?.code || selected.suggestions[0]?.code || '')
    setNote('')
    setActionError('')
  }, [selected])

  const claim = async () => {
    if (!selected) return
    setBusy(true)
    setActionError('')
    try {
      const next = await claimReviewItem(selected.id)
      state.setData((current) => current ? { ...current, items: replaceItem(current.items, next) } : current)
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : '领取失败')
    } finally {
      setBusy(false)
    }
  }

  const decide = async (decision: ReviewDecisionInput['decision']) => {
    if (!selected || !selected.claimed_by_me) return
    setBusy(true)
    setActionError('')
    try {
      const next = await decideReviewItem(selected.id, {
        decision,
        ...(decision === 'confirm' && suggestionCode ? { suggestion_code: suggestionCode } : {}),
        ...(note.trim() ? { note: note.trim() } : {}),
      })
      state.setData((current) => current ? { ...current, items: replaceItem(current.items, next) } : current)
      setSelectedId('')
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : '提交失败')
    } finally {
      setBusy(false)
    }
  }

  return <div className="cc-page">
    <PageIntro title="确认收件箱" description="每项都包含原始证据、系统建议和影响范围。确认只处理本次事项，不会自动发布通用规则。" />
    {state.loading ? <ControlLoading label="正在取得待确认事项…" /> : null}
    {state.error ? <ControlError message={state.error} onRetry={state.reload} /> : null}
    {!state.loading && !state.error && items.length === 0 ? <ControlEmpty title="目前没有待确认事项" description="无法唯一判断的数据或核对差异会进入这里。" /> : null}
    {items.length > 0 ? <div className={`cc-inbox ${selected ? 'cc-inbox--detail' : ''}`}>
      <section className="cc-inbox-list" aria-label="待确认列表">
        <h2>待确认 <span>{items.length}</span></h2>
        {items.map((item) => <button
          type="button"
          key={item.id}
          className={item.id === selectedId ? 'is-selected' : ''}
          onClick={() => setSelectedId(item.id)}
          aria-current={item.id === selectedId ? 'true' : undefined}
        >
          <div><strong>{item.title}</strong>{item.risk === 'high' ? <StatusMark tone="warning">需复核</StatusMark> : null}</div>
          <p>{item.summary}</p>
          <small>{formatDateTime(item.created_at)}</small>
        </button>)}
      </section>
      <section className="cc-inbox-detail" aria-label="确认详情">
        {selected ? <>
          <button type="button" className="cc-mobile-back" onClick={() => setSelectedId('')}><ArrowLeft aria-hidden="true" />返回待确认列表</button>
          <div className="cc-section-heading"><div><h2>{selected.title}</h2><p>{selected.summary}</p></div>{selected.risk === 'high' ? <StatusMark tone="warning">高影响事项</StatusMark> : <StatusMark tone="neutral">普通事项</StatusMark>}</div>
          <section className="cc-evidence" aria-labelledby="evidence-title">
            <h3 id="evidence-title">核对证据</h3>
            <dl>{selected.evidence.map((evidence) => <div key={`${evidence.label}-${evidence.value}`}><dt>{evidence.label}</dt><dd>{evidence.value}</dd>{evidence.source_reference ? <small>{evidence.source_reference}</small> : null}</div>)}</dl>
          </section>
          <fieldset className="cc-suggestions" disabled={!selected.claimed_by_me || busy}>
            <legend>系统建议</legend>
            {selected.suggestions.map((suggestion) => <label key={suggestion.code}>
              <input type="radio" name={`suggestion-${selected.id}`} value={suggestion.code} checked={suggestionCode === suggestion.code} onChange={(event) => setSuggestionCode(event.target.value)} />
              <span><strong>{suggestion.label}{suggestion.recommended ? '（建议）' : ''}</strong><small>{suggestion.explanation}</small></span>
            </label>)}
          </fieldset>
          <label className="cc-field">补充说明（可选）<textarea value={note} onChange={(event) => setNote(event.target.value)} disabled={!selected.claimed_by_me || busy} rows={3} /></label>
          {selected.risk === 'high' ? <p className="cc-inline-warning"><LockKeyhole aria-hidden="true" />此事项需要另一位有审批权限的人员复核后才会生效。</p> : null}
          {actionError ? <p className="cc-form-error" role="alert"><CircleAlert aria-hidden="true" />{actionError}</p> : null}
          <div className="cc-inbox-actions">
            {!selected.claimed_by_me ? <button type="button" className="cc-button cc-button--primary" onClick={() => void claim()} disabled={busy || (selected.status === 'claimed' && Boolean(selected.claimed_by_name))}>
              {busy ? '正在领取…' : selected.status === 'claimed' ? `由 ${selected.claimed_by_name || '其他人员'} 处理中` : '领取并处理'}
            </button> : <>
              <button type="button" className="cc-button cc-button--primary" onClick={() => void decide('confirm')} disabled={busy || !suggestionCode}><Check aria-hidden="true" />{busy ? '正在提交…' : '确认此处理'}</button>
              <button type="button" className="cc-button cc-button--danger" onClick={() => void decide('reject')} disabled={busy}><X aria-hidden="true" />拒绝并保留证据</button>
            </>}
          </div>
        </> : <ControlEmpty title="选择一项开始处理" description="请先从左侧选择待确认事项。" />}
      </section>
    </div> : null}
  </div>
}
