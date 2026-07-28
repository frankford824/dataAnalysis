import { Download } from 'lucide-react'
import { useState } from 'react'
import { getOverview, exportCertified } from '../api/analytics'
import { EmptyState, ErrorState, LoadingState } from '../components/AsyncState'
import BusinessFilters from '../components/BusinessFilters'
import PageHeader from '../components/PageHeader'
import { useFilters } from '../context/FilterContext'
import { useRequest } from '../hooks/useRequest'
import type { DashboardSummary } from '../types'

const currency = new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY', minimumFractionDigits: 2 })

function TrendChart({ values }: { values: DashboardSummary['trend'] }) {
  if (values.length < 2) return <p className="chart-empty">当前范围不足两个有数据的日期，暂不显示趋势线。</p>
  const width = 780
  const height = 220
  const maximum = Math.max(1, ...values.flatMap((value) => [value.revenue, value.profit]))
  const points = (key: 'revenue' | 'profit') => values.map((value, index) => {
    const x = 24 + index * ((width - 48) / (values.length - 1))
    const y = height - 34 - (value[key] / maximum) * (height - 62)
    return `${x},${y}`
  }).join(' ')
  return <div className="chart-wrap"><svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="净销售额与经营利润趋势">
    {[0, 1, 2, 3].map((line) => <line key={line} x1="24" y1={28 + line * 47} x2="756" y2={28 + line * 47} stroke="#e6ebf2" />)}
    <polyline points={points('revenue')} fill="none" stroke="#1769ff" strokeWidth="3" />
    <polyline points={points('profit')} fill="none" stroke="#00a881" strokeWidth="3" />
    {values.map((value, index) => <text key={value.month} x={24 + index * ((width - 48) / (values.length - 1))} y="212" textAnchor="middle" fontSize="11" fill="#667085">{value.month.slice(5)}</text>)}
  </svg></div>
}

export default function DashboardPage() {
  const { filters } = useFilters()
  const [format, setFormat] = useState<'csv' | 'xlsx'>('xlsx')
  const [exportError, setExportError] = useState('')
  const state = useRequest(() => getOverview(filters), [filters.platformId, filters.dateFrom, filters.dateTo, filters.storeIds.join(',')])
  const exportData = async () => { setExportError(''); try { await exportCertified(filters, format) } catch (error) { setExportError(error instanceof Error ? error.message : '导出失败') } }
  return <>
    <PageHeader title="经营看板" description="只展示已经核对并发布的经营数据。" action={<div className="export-controls"><select aria-label="导出格式" value={format} onChange={(event) => setFormat(event.target.value as 'csv' | 'xlsx')}><option value="xlsx">Excel</option><option value="csv">CSV</option></select><button className="button primary" onClick={() => void exportData()}><Download size={18} />导出当前范围</button></div>} />
    {exportError ? <p className="form-error" role="alert">{exportError}</p> : null}
    <BusinessFilters />
    {state.loading ? <LoadingState label="正在汇总经营结果…" /> : null}
    {state.error ? <ErrorState message={state.error} retry={state.reload} /> : null}
    {!state.loading && !state.error && state.data?.stores.length === 0 ? <EmptyState title="当前范围还没有已发布数据" description="完成本月数据核对和发布后，经营结果会显示在这里。" /> : null}
    {state.data && state.data.stores.length > 0 ? <DashboardContent data={state.data} /> : null}
  </>
}

function DashboardContent({ data }: { data: DashboardSummary }) {
  const metrics = [['销售', data.revenue, 'currency'], ['退款', data.refund, 'currency'], ['费用', data.fees, 'currency'], ['成本', data.cost, 'currency'], ['经营利润', data.profit, 'currency'], ['订单数', data.order_count, 'count']] as const
  return <>
    <section className="metric-strip">{metrics.map(([label, value, kind]) => <article className="metric" key={label}><div><span>{label}</span><strong>{kind === 'count' ? value.toLocaleString('zh-CN') : currency.format(value)}</strong></div></article>)}</section>
    <section className="panel chart-panel"><div className="panel-title"><h2>净销售额与经营利润趋势</h2><div className="legend"><span className="rev" />净销售额 <span className="profit" />经营利润</div></div><TrendChart values={data.trend} /></section>
    <section className="panel table-panel"><h2>店铺经营表现</h2><div className="table-scroll"><table><thead><tr><th>店铺</th><th>订单数</th><th>销售</th><th>退款</th><th>费用</th><th>成本</th><th>经营利润</th><th>利润率</th></tr></thead><tbody>{data.stores.map((store) => <tr key={store.id}><td>{store.name}</td><td>{store.order_count.toLocaleString('zh-CN')}</td><td>{currency.format(store.revenue)}</td><td className="refund-value">{currency.format(store.refund)}</td><td>{currency.format(store.fees)}</td><td>{currency.format(store.cost)}</td><td>{currency.format(store.profit)}</td><td>{store.profit_margin.toFixed(2)}%</td></tr>)}</tbody></table></div></section>
  </>
}
