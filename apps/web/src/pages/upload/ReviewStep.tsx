import { CheckCircle2, CopyCheck } from 'lucide-react'
import type { IngestionRun } from '../../types'

type Check = { key: string; applicable?: boolean; status: 'passed' | 'failed' | 'not_applicable'; actual?: unknown; minimum?: unknown; maximum?: unknown }
const moneyKeys = new Set(['revenue', 'refund', 'fees', 'product_cost', 'profit'])
const summaryFields = [
  ['store_count', '识别的店铺'], ['coverage', '覆盖日期'], ['order_count', '订单数'], ['revenue', '销售'], ['refund', '退款'], ['fees', '费用'], ['product_cost', '成本'], ['profit', '经营利润'], ['duplicate_rows_removed', '重复记录'],
] as const
const checkLabels: Record<string, string> = {
  file_completeness: '文件是否完整', row_count: '记录数量', order_count: '订单数量', revenue: '销售金额', refund: '退款金额', fees: '费用金额', duplicate_file: '重复文件', duplicate_rows: '重复记录', cross_source_match: '跨来源关联', unexplained_difference: '无法解释的差额', coverage_period: '覆盖日期', valid_time: '有效业务日期', amount_format: '金额格式', store_scope: '店铺范围', duplicate_business_key: '重复业务记录', expected_volume: '文件记录数量符合预期', cross_source_reconciliation: '跨文件核对', semantic_model: '标准经营模型',
}

function checkLabel(key: string) {
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
  if (key === 'store_count' && storeName) return storeName
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

export default function ReviewStep({ run, storeName, duplicate, busy, note, onNote, onPublish }: { run: IngestionRun; storeName?: string; duplicate: boolean; busy: boolean; note: string; onNote: (value: string) => void; onPublish: () => void }) {
  const checks = checksOf(run)
  if (duplicate) return <section className="flow-panel review-result"><CopyCheck className="result-icon" /><h2>这份文件已经处理过</h2><p>{run.original_filename} 不会再次计入经营结果。</p><a className="button primary" href="/dashboard">查看经营看板</a></section>
  if (['published', 'locked'].includes(run.status)) return <section className="flow-panel review-result"><CheckCircle2 className="result-icon" /><h2>经营看板已经更新</h2><p>这份文件已完成核对并进入正式经营结果。</p><a className="button primary" href="/dashboard">查看经营看板</a></section>
  return <section className="flow-panel"><h2>核对并更新</h2><p>确认摘要和检查结果无误后，再更新正式经营看板。</p><dl className="review-list"><div><dt>文件</dt><dd>{run.original_filename}</dd></div>{summaryFields.map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{summaryValue(run, key, storeName)}</dd></div>)}</dl>{checks.length ? <div className="quality-list"><h3>经营数据检查</h3>{checks.map((check) => <p key={check.key} className={check.status}><span>{checkLabel(check.key)}{checkDetail(check) ? <small>{checkDetail(check)}</small> : null}</span><strong>{check.status === 'passed' ? '通过' : check.status === 'failed' ? '需处理' : '不适用'}</strong></p>)}</div> : null}<label>补充说明（可选）<textarea value={note} onChange={(event) => onNote(event.target.value)} placeholder="例如：本月平台有一次补扣费用" /></label><button className="button primary flow-action" disabled={busy || checks.some((check) => check.status === 'failed')} onClick={onPublish}>{busy ? '正在更新看板…' : '确认并更新看板'}</button></section>
}
