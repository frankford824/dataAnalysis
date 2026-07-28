import { CheckCircle2, CircleAlert, FileText } from 'lucide-react'
import { Link } from 'react-router-dom'
import { getCompleteness } from '../api/analytics'
import { listIngestions } from '../api/ingestions'
import { ErrorState, LoadingState } from '../components/AsyncState'
import PageHeader from '../components/PageHeader'
import { useAuth } from '../context/AuthContext'
import { useFilters } from '../context/FilterContext'
import { useRequest } from '../hooks/useRequest'

export default function HomePage() {
  const auth = useAuth()
  const { filters } = useFilters()
  const state = useRequest(() => Promise.all([getCompleteness(filters), listIngestions()]), [filters])
  if (state.loading) return <LoadingState label="正在准备首页…" />
  if (state.error || !state.data) return <ErrorState message={state.error || '没有返回内容'} retry={state.reload} />

  const [completeness, runs] = state.data
  const month = filters.dateFrom.slice(0, 7)
  const monthLabel = `${Number(month.slice(0, 4))}年${Number(month.slice(5))}月`
  const currentRuns = runs.filter((run) =>
    (run.coverage_start || run.created_at || '').slice(0, 7) === month
    && ['published', 'locked'].includes(run.status),
  )
  const missing = completeness?.missing ?? []
  const complete = completeness?.status === 'complete'

  return <>
    <PageHeader title="首页" description={`当前经营期间：${monthLabel}`} />
    <section className={`month-readiness ${complete ? 'complete' : 'incomplete'}`}>
      <h2>本月数据是否完整</h2>
      <div className="readiness-value">{complete ? <CheckCircle2 /> : <CircleAlert />}<strong>{monthLabel}数据{complete ? '已完整' : '尚未完整'}</strong></div>
    </section>
    {!complete ? <section className="open-list"><h2>还需要做什么</h2>{missing.length > 0 ? missing.map((item) => <div key={`${item.source_logical_id}:${item.store_logical_id}`}><FileText /><span>{item.store_name}还需添加{item.source_name}并通过核对发布</span></div>) : <p>管理员尚未设置本月需要的数据内容和覆盖店铺。</p>}{auth.canUpload && missing.length > 0 ? <Link className="button primary main-home-action" to="/data">添加本月数据</Link> : null}</section> : null}
    <section className="open-list completed-list"><h2>已完成事项</h2>{currentRuns.length === 0 ? <p>本月还没有完成的文件。</p> : currentRuns.map((run) => <div key={run.id}><CheckCircle2 /><span>{run.original_filename} · {statusLabel(run.status)}</span></div>)}</section>
    {!auth.canUpload ? <p className="restricted-note">您可以查看看板、提问和导出；数据准备由企业管理员或实施人员完成。</p> : null}
  </>
}

function statusLabel(status: string) {
  if (['published', 'locked'].includes(status)) return '已更新看板'
  if (status === 'confirmed') return '已确认'
  return '已收到'
}
