import { CheckCircle2, Circle } from 'lucide-react'
import { Link } from 'react-router-dom'
import { listDashboards, listSources, listStores } from '../../api/resources'
import { listUsers } from '../../api/admin'
import { ErrorState, LoadingState } from '../../components/AsyncState'
import PageHeader from '../../components/PageHeader'
import { useRequest } from '../../hooks/useRequest'

export default function StartPage() {
  const state = useRequest(() => Promise.all([listStores(), listSources(), listUsers(), listDashboards()]), [])
  if (state.loading) return <LoadingState label="正在检查首次设置…" />
  if (state.error || !state.data) return <ErrorState message={state.error || '无法检查设置'} retry={state.reload} />
  const [stores, sources, users, dashboards] = state.data
  const checks = [
    { label: '添加至少一家店铺', done: stores.length > 0, to: '/admin/data' },
    { label: '设置需要收集的数据', done: sources.length > 0, to: '/admin/data' },
    { label: '邀请业务用户', done: users.length > 1, to: '/admin/users' },
    { label: '发布首个经营看板', done: dashboards.some((item) => ['active', 'published'].includes(item.status || '')), to: '/admin/reports' },
  ]
  const completed = checks.filter((check) => check.done).length
  return <>
    <PageHeader title="开始使用" description={`已完成 ${completed}/${checks.length} 项。完成这些设置后，业务用户即可开始使用。`} />
    <section className="setup-checklist">{checks.map((check) => <Link to={check.to} key={check.label}>{check.done ? <CheckCircle2 className="success" /> : <Circle />}<span>{check.label}</span><strong>{check.done ? '已完成' : '去设置'}</strong></Link>)}</section>
  </>
}
