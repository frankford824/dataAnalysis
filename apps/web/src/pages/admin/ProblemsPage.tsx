import { useEffect, useState } from 'react'
import { listProblems, updateIssue } from '../../api/admin'
import { listSources } from '../../api/resources'
import { EmptyState, ErrorState, LoadingState } from '../../components/AsyncState'
import PageHeader from '../../components/PageHeader'
import { useRequest } from '../../hooks/useRequest'

const kindLabels: Record<string, string> = {
  source_not_recognized: '无法识别文件类型', source_ambiguous: '文件类型需要确认', file_validation: '文件内容需要调整', quality_gate: '经营数据检查未通过', cross_source_reconciliation: '跨文件核对未通过',
}
const fieldLabels: Record<string, string> = {
  order_id: '订单号', occurred_at: '业务日期', store_id: '店铺', revenue: '销售金额', refund: '退款金额', platform_fee: '平台费用', advertising_fee: '广告费用', shipping_fee: '运费', product_cost: '商品成本',
}

function textDetail(details: Record<string, unknown> | undefined, key: string) {
  const value = details?.[key]
  if (Array.isArray(value)) return value.map(String).join('、')
  return typeof value === 'string' ? value : ''
}

export default function ProblemsPage() {
  const state = useRequest(() => Promise.all([listProblems(), listSources()]), [])
  const [selectedId, setSelectedId] = useState('')
  const [uploadedField, setUploadedField] = useState('')
  const [canonicalField, setCanonicalField] = useState('revenue')
  const [sourceId, setSourceId] = useState('')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const issues = state.data?.[0] || []
  useEffect(() => { if (!selectedId && issues[0]) setSelectedId(issues[0].id) }, [issues, selectedId])
  const selected = issues.find((issue) => issue.id === selectedId) || issues[0]

  const act = async (reject = false) => {
    if (!selected || (!reject && !sourceId)) return
    setBusy(true)
    setMessage('')
    try {
      const retry = Boolean(uploadedField.trim())
      await updateIssue(selected.id, {
        action: reject ? 'reject' : retry ? 'retry_with_mapping' : 'resolve',
        resolution: reject ? '已确认拒绝此文件，不进入经营结果' : '已确认文件类型和字段含义，等待重新上传检查',
        ...(!reject ? { source_definition_id: sourceId } : {}),
        ...(retry ? { field_mapping: { [canonicalField]: uploadedField.trim() } } : {}),
      })
      setSelectedId(''); setUploadedField(''); setSourceId(''); state.reload()
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : '处理失败')
    } finally { setBusy(false) }
  }

  return <>
    <PageHeader title="待处理问题" description="选择一个问题，在右侧完成必要的业务确认。" />
    {state.loading ? <LoadingState /> : null}{state.error ? <ErrorState message={state.error} retry={state.reload} /> : null}
    {message ? <p className="form-error" role="alert">{message}</p> : null}
    {!state.loading && issues.length === 0 ? <EmptyState title="没有待处理问题" description="新的识别或经营数据问题会显示在这里。" /> : null}
    {selected && state.data ? <div className="issues-workspace">
      <section className="issues-list" aria-label="问题列表">{issues.map((issue) => <button key={issue.id} className={issue.id === selected.id ? 'selected' : ''} onClick={() => { setSelectedId(issue.id); setUploadedField(''); setSourceId('') }}><strong>{kindLabels[issue.kind] || '数据需要确认'}</strong><span>{textDetail(issue.technical_detail, 'filename') || '本月经营数据'}</span><small>{issue.user_message}</small></button>)}</section>
      <section className="issue-detail"><h2>{kindLabels[selected.kind] || '数据需要确认'}</h2><h3>业务摘要</h3><p>{selected.user_message}</p><dl><div><dt>文件</dt><dd>{textDetail(selected.technical_detail, 'filename') || '—'}</dd></div><div><dt>发生时间</dt><dd>{selected.created_at?.replace('T', ' ').slice(0, 16)}</dd></div><div><dt>补充原因</dt><dd>{textDetail(selected.technical_detail, 'reason') || textDetail(selected.technical_detail, 'failed_checks') || '需要人工确认文件类型或字段含义'}</dd></div></dl>
        <label>对应的数据内容<select value={sourceId} onChange={(event) => setSourceId(event.target.value)}><option value="">请选择数据内容</option>{state.data[1].map((source) => <option key={source.id} value={source.id}>{source.name}</option>)}</select></label>
        <div className="mapping-fields"><label>文件中的列名（可选）<input value={uploadedField} onChange={(event) => setUploadedField(event.target.value)} placeholder="例如：实付金额" /></label><label>这一列表示<select value={canonicalField} onChange={(event) => setCanonicalField(event.target.value)}>{Object.entries(fieldLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label></div>
        <p className="field-help">填写列名后，系统会保存字段映射并在重新上传时按确定性规则检查。</p>
        <div className="issue-actions"><button className="button primary" disabled={busy || !sourceId} onClick={() => void act()}>{busy ? '正在处理…' : uploadedField.trim() ? '保存映射并重新检查' : '保存并等待重新上传'}</button><button className="button destructive" disabled={busy} onClick={() => void act(true)}>拒绝此文件</button></div>
      </section>
    </div> : null}
  </>
}
