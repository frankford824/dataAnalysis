import { ArrowRight, CheckCircle2, CircleAlert, Clock3, MonitorCog } from 'lucide-react'
import { getControlOverview } from './api'
import { ControlEmpty, ControlError, ControlLoading, ControlOffline } from './components/ControlStates'
import { connectorStatusLabel, formatDateTime, operationStatusLabel, PageIntro, StatusMark, statusTone } from './components/ControlPrimitives'
import { useControlRequest } from './hooks/useControlRequest'
import './control-center.css'

export default function WorkCenterPage() {
  const state = useControlRequest(getControlOverview)

  if (state.loading) return <ControlLoading label="正在取得工作进展…" />
  if (state.error) return <ControlError message={state.error} onRetry={state.reload} />
  if (!state.data) return <ControlEmpty title="暂时没有工作信息" description="系统取得第一条连接状态后会显示在这里。" />

  const { connector, current_operation: operation, latest_result: result, pending_review_count: pending, primary_action: action } = state.data
  const hostOffline = connector.status === 'offline' || connector.status === 'permission_denied' || connector.status === 'failed'

  return <div className="cc-page">
    <PageIntro
      title="工作台"
      description="在这里查看 finance-win 是否在线、数据处理到了哪里，以及现在需要你做什么。"
      action={action.kind !== 'none' ? <a className="cc-button cc-button--primary" href={action.target}>{action.label}<ArrowRight aria-hidden="true" /></a> : undefined}
    />

    {hostOffline ? <ControlOffline
      title={connector.status === 'permission_denied' ? 'finance-win 的读取权限不足' : 'finance-win 当前不可用'}
      description={connector.message || '系统保留最近一次有效结果，主机恢复连接后会继续。'}
      action={<a className="cc-link-button" href="/control/data-sources">查看连接详情</a>}
    /> : null}

    <section className="cc-summary-strip" aria-label="当前概况">
      <article>
        <span className="cc-summary-icon"><MonitorCog aria-hidden="true" /></span>
        <div><small>finance-win</small><strong>{connectorStatusLabel(connector.status)}</strong><p>{connector.machine_name} · 最近联系 {formatDateTime(connector.last_seen_at)}</p></div>
      </article>
      <article>
        <span className="cc-summary-icon"><Clock3 aria-hidden="true" /></span>
        <div><small>当前处理</small><strong>{operation ? operationStatusLabel(operation.status) : '没有运行中的任务'}</strong><p>{operation?.title || '系统会自动发现符合读取范围的新文件'}</p></div>
      </article>
      <article>
        <span className={`cc-summary-icon ${pending > 0 ? 'cc-summary-icon--attention' : ''}`}><CircleAlert aria-hidden="true" /></span>
        <div><small>待你确认</small><strong>{pending} 项</strong><p>{pending > 0 ? '确认后相关任务才能继续' : '目前不需要人工处理'}</p></div>
      </article>
    </section>

    <div className="cc-work-grid">
      <section className="cc-section" aria-labelledby="current-operation-title">
        <div className="cc-section-heading"><div><h2 id="current-operation-title">当前任务</h2><p>所有阶段均来自服务端和外部执行器。</p></div>{operation ? <StatusMark tone={statusTone(operation.status)}>{operationStatusLabel(operation.status)}</StatusMark> : null}</div>
        {operation ? <>
          <dl className="cc-facts cc-facts--compact">
            <div><dt>当前阶段</dt><dd>{operation.stage}</dd></div>
            <div><dt>外部执行器</dt><dd>{operation.worker_online ? `${operation.worker_name || 'finance-win'} 在线` : '离线'}</dd></div>
            <div><dt>最近更新</dt><dd>{formatDateTime(operation.updated_at)}</dd></div>
            <div><dt>等待确认</dt><dd>{operation.waiting_review_count ?? 0} 项</dd></div>
          </dl>
          {operation.blocking_reason ? <p className="cc-inline-warning"><CircleAlert aria-hidden="true" />{operation.blocking_reason}</p> : null}
          <a className="cc-text-action" href={`/control/operations?operation=${encodeURIComponent(operation.id)}`}>查看处理时间线<ArrowRight aria-hidden="true" /></a>
        </> : <ControlEmpty title="没有运行中的任务" description="新的文件被发现后，任务会自动出现在这里。" />}
      </section>

      <section className="cc-section" aria-labelledby="latest-result-title">
        <div className="cc-section-heading"><div><h2 id="latest-result-title">最近结果</h2><p>只展示服务端已经形成的结果，不在浏览器中补算金额。</p></div>{result?.status === 'certified' ? <StatusMark tone="success">已认证</StatusMark> : null}</div>
        {result ? <>
          <div className="cc-result-title"><CheckCircle2 aria-hidden="true" /><div><strong>{result.title}</strong><span>完成于 {formatDateTime(result.completed_at)}</span></div></div>
          <dl className="cc-facts">{result.facts.map((fact) => <div key={`${fact.label}-${fact.value}`}><dt>{fact.label}</dt><dd>{fact.value}</dd>{fact.note ? <small>{fact.note}</small> : null}</div>)}</dl>
        </> : <ControlEmpty title="还没有完成的结果" description="首个任务通过确认和认证门禁后会显示在这里。" />}
      </section>
    </div>
  </div>
}
