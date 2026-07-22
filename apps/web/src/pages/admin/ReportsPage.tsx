import { ExternalLink } from 'lucide-react'
import { listDashboards } from '../../api/resources'
import { EmptyState, ErrorState, LoadingState } from '../../components/AsyncState'
import PageHeader from '../../components/PageHeader'
import { useRequest } from '../../hooks/useRequest'

export default function ReportsPage() {
  const state = useRequest(listDashboards, [])
  const supersetUrl = import.meta.env.VITE_SUPERSET_URL
  const designAction = supersetUrl ? <a className="button primary" href={supersetUrl} target="_blank" rel="noreferrer">打开设计环境<ExternalLink size={17} /></a> : <span className="disabled-capability">设计环境尚未启用</span>
  return <>
    <PageHeader title="高级报表设计" description="在 Superset 中设计图表和看板；业务用户只会看到已经发布的内容。" action={designAction} />
    {state.loading ? <LoadingState /> : null}
    {state.error ? <ErrorState message={state.error} retry={state.reload} /> : null}
    {state.data?.length === 0 ? <EmptyState title="还没有报表" description={supersetUrl ? '在设计环境中创建并发布首个经营看板。' : '请先由部署管理员启用 Superset。'} /> : null}
    {state.data && state.data.length > 0 ? <section className="report-list">{state.data.map((dashboard) => <article key={dashboard.id}><div><h2>{dashboard.name}</h2><p>{dashboard.status === 'published' || dashboard.status === 'active' ? '业务用户可见' : '尚未发布'}</p></div>{dashboard.embed_url ? <a className="button secondary" href={String(dashboard.embed_url)} target="_blank" rel="noreferrer">预览<ExternalLink size={16} /></a> : <span>暂无预览</span>}</article>)}</section> : null}
  </>
}
