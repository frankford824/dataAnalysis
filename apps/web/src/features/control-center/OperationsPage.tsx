import { CircleAlert, Clock3, RefreshCw, ServerOff, Wifi } from 'lucide-react'
import { ControlEmpty, ControlError, ControlLoading } from './components/ControlStates'
import { connectionStatusLabel, formatDateTime, operationStatusLabel, PageIntro, StatusMark, statusTone } from './components/ControlPrimitives'
import { useOperationFeed } from './hooks/useOperationFeed'
import './control-center.css'

export default function OperationsPage() {
  const state = useOperationFeed()

  return <div className="cc-page">
    <PageIntro
      title="处理进度"
      description="进度来自控制服务和 finance-win 外部执行器；连接中断时会停止实时动画并改为定时刷新。"
      action={<button type="button" className="cc-button cc-button--secondary" onClick={() => void state.reload()} disabled={state.loading}><RefreshCw aria-hidden="true" />立即刷新</button>}
    />
    <div className={`cc-connection cc-connection--${state.connection}`} role="status">
      {state.connection === 'offline' ? <ServerOff aria-hidden="true" /> : <Wifi aria-hidden="true" />}
      <strong>{connectionStatusLabel(state.connection)}</strong>
      <span>最近更新：{formatDateTime(state.lastUpdatedAt || undefined)}</span>
    </div>
    {state.loading ? <ControlLoading label="正在取得处理进度…" /> : null}
    {state.error && !state.data ? <ControlError message={state.error} onRetry={() => void state.reload()} /> : null}
    {state.error && state.data ? <p className="cc-inline-warning" role="alert"><CircleAlert aria-hidden="true" />{state.error}</p> : null}
    {!state.loading && state.data?.items.length === 0 ? <ControlEmpty title="没有处理记录" description="系统发现符合范围的新文件后会自动创建处理任务。" /> : null}
    {state.data?.items.map((operation) => <article className="cc-operation" key={operation.id}>
      <div className="cc-section-heading">
        <div><h2>{operation.title}</h2><p>{operation.stage} · 最近更新 {formatDateTime(operation.updated_at)}</p></div>
        <StatusMark tone={statusTone(operation.status)}>{operationStatusLabel(operation.status)}</StatusMark>
      </div>
      <dl className="cc-operation-counts">
        <div><dt>发现</dt><dd>{operation.discovered_count ?? 0}</dd></div>
        <div><dt>已处理</dt><dd>{operation.processed_count ?? 0}</dd></div>
        <div><dt>待确认</dt><dd>{operation.waiting_review_count ?? 0}</dd></div>
        <div><dt>失败</dt><dd>{operation.failed_count ?? 0}</dd></div>
      </dl>
      {!operation.worker_online ? <p className="cc-inline-warning"><ServerOff aria-hidden="true" />外部执行器已离线。{operation.blocking_reason || '恢复连接后任务会从已记录阶段继续。'}</p> : null}
      <ol className="cc-timeline" aria-label={`${operation.title}时间线`}>
        {operation.timeline.map((event) => <li key={event.id}>
          <span className="cc-timeline-dot" aria-hidden="true" />
          <div><div><strong>{event.stage}</strong><time dateTime={event.occurred_at}>{formatDateTime(event.occurred_at)}</time></div><p>{event.message}</p></div>
        </li>)}
      </ol>
      {operation.timeline.length === 0 ? <p className="cc-muted"><Clock3 aria-hidden="true" />等待第一条处理记录</p> : null}
    </article>)}
  </div>
}
