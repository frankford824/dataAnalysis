import { CheckCircle2, CopyCheck } from 'lucide-react'
import { Link } from 'react-router-dom'
import type { IngestionRun } from '../../types'

type Check = { key: string; applicable?: boolean; status: 'passed' | 'failed' | 'pending' | 'not_applicable'; message?: string; actual?: unknown; minimum?: unknown; maximum?: unknown }
const moneyKeys = new Set(['revenue', 'refund', 'fees', 'product_cost', 'profit'])
const summaryFields = [
  ['store_count', '识别的店铺'], ['coverage', '覆盖日期'], ['order_count', '订单数'], ['revenue', '销售'], ['refund', '退款'], ['fees', '费用'], ['product_cost', '成本'], ['profit', '经营利润'], ['duplicate_rows_removed', '重复记录'],
] as const
const checkLabels: Record<string, string> = {
  file_completeness: '文件是否完整', row_count: '记录数量', order_count: '订单数量', revenue: '销售金额', refund: '退款金额', fees: '费用金额', duplicate_file: '重复文件', duplicate_rows: '重复记录', cross_source_match: '跨来源关联', unexplained_difference: '无法解释的差额', coverage_period: '覆盖日期', valid_time: '有效业务日期', amount_format: '金额格式', store_scope: '店铺范围', duplicate_business_key: '重复业务记录', expected_volume: '文件记录数量符合预期', cross_source_reconciliation: '跨文件核对', semantic_model: '标准经营模型',
}

function checkLabel(key: string) {
  if (key.startsWith('cross_source:')) return '本月必需文件已齐全并核对'
  if (key.startsWith('required_field:')) return `必填业务字段：${key.split(':', 2)[1]}`
  if (key.startsWith('control_total:')) return `控制总额：${key.split(':', 2)[1]}`
  if (key.startsWith('non_negative:')) return `金额方向：${key.split(':', 2)[1]}`
  return checkLabels[key] || key
}

function checkDetail(check: Check) {
  if (check.status !== 'failed') return ''
  if (check.key === 'expected_volume') return `实际 ${check.actual ?? '—'} 条，预期 ${check.minimum ?? '—'} 至 ${check.maximum ?? '—'} 条`
  if (check.key.startsWith('required_field:')) return '文件中缺少该业务字段'
  return '请联系实施人员核对数据设置'
}

function summaryValue(run: IngestionRun, key: string, storeName?: string) {
  const summary = run.summary || {}
  if (key === 'store_count') {
    if (storeName) return storeName
    const storeIds = summary.store_ids
    if (Array.isArray(storeIds) && storeIds.length) return `${storeIds.length} 个店铺`
  }
  if (key === 'coverage') return `${run.coverage_start?.slice(0, 10) || '待确认'} 至 ${run.coverage_end?.slice(0, 10) || '待确认'}`
  const value = summary[key]
  if (value === undefined || value === null) return '—'
  if (moneyKeys.has(key)) return Number(value).toLocaleString('zh-CN', { style: 'currency', currency: 'CNY', minimumFractionDigits: 2 })
  if (key === 'duplicate_rows_removed') return `${value} 条（已跳过，不会重复计算）`
  return String(value)
}

function checksOf(run: IngestionRun) {
  const raw = run.quality_result?.checks
  return Array.isArray(raw) ? raw.filter((item): item is Check => Boolean(item && typeof item === 'object' && 'key' in item && 'status' in item)) : []
}

type ReviewStepProps = {
  run: IngestionRun
  storeName?: string
  duplicate: boolean
  busy: boolean
  note: string
  onNote: (value: string) => void
  onPublish: () => void
  onAddAnother?: () => void
  onContinuePending?: () => void
  hasPending?: boolean
  correctionRequired?: boolean
  canCorrect?: boolean
  correctionReason?: string
  onCorrectionReason?: (value: string) => void
  onCorrect?: () => void
}

export default function ReviewStep({ run, storeName, duplicate, busy, note, onNote, onPublish, onAddAnother, onContinuePending, hasPending, correctionRequired, canCorrect, correctionReason = '', onCorrectionReason, onCorrect }: ReviewStepProps) {
  const checks = checksOf(run)
  const waiting = checks.some((check) => check.status === 'pending')
  if (duplicate) return <section className="flow-panel review-result"><CopyCheck className="result-icon" /><h2>这份文件已经处理过</h2><p>{run.original_filename} 不会再次计入经营结果。</p><Link className="button primary" to="/dashboard">查看经营看板</Link></section>
  if (['published', 'locked'].includes(run.status)) return <section className="flow-panel review-result"><CheckCircle2 className="result-icon" /><h2>经营看板已经更新</h2><p>这份文件已完成核对并进入正式经营结果。</p>{hasPending && onContinuePending ? <button className="button primary" onClick={onContinuePending}>继续核对其他文件</button> : <Link className="button primary" to="/dashboard">查看经营看板</Link>}</section>
  return <section className="flow-panel"><h2>核对并更新</h2><p>确认摘要和检查结果无误后，再更新正式经营看板。</p><dl className="review-list"><div><dt>文件</dt><dd>{run.original_filename}</dd></div>{summaryFields.map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{summaryValue(run, key, storeName)}</dd></div>)}</dl>{checks.length ? <div className="quality-list"><h3>经营数据检查</h3>{checks.map((check) => <p key={check.key} className={check.status}><span>{checkLabel(check.key)}{check.message || checkDetail(check) ? <small>{check.message || checkDetail(check)}</small> : null}</span><strong>{check.status === 'passed' ? '通过' : check.status === 'failed' ? '需处理' : check.status === 'pending' ? '等待文件' : '不适用'}</strong></p>)}</div> : null}<label>补充说明（可选）<textarea value={note} onChange={(event) => onNote(event.target.value)} placeholder="例如：本月平台有一次补扣费用" /></label>{correctionRequired ? <div className="correction-panel" role="alert"><h3>这个月份已经锁定</h3>{canCorrect ? <><p>更正会保留旧文件和审计记录，并用本次结果替换当前看板。请填写业务原因。</p><label>更正原因<textarea value={correctionReason} onChange={(event) => onCorrectionReason?.(event.target.value)} placeholder="例如：平台在月结后补发了修订订单文件" /></label><button className="button primary flow-action" disabled={busy || correctionReason.trim().length < 10} onClick={onCorrect}>{busy ? '正在更正…' : '管理员确认更正并更新'}</button></> : <p>请由企业管理员确认更正原因后再更新。旧结果不会被静默覆盖。</p>}</div> : waiting && onAddAnother ? <button className="button primary flow-action" onClick={onAddAnother}>添加缺少的本月文件</button> : <button className="button primary flow-action" disabled={busy || checks.some((check) => check.status === 'failed' || check.status === 'pending')} onClick={onPublish}>{busy ? '正在更新看板…' : '确认并更新看板'}</button>}</section>
}
