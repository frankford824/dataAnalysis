import { Database, FileBarChart2, FolderSearch, History, Monitor, RefreshCw, ShieldCheck } from 'lucide-react'
import { listConnectors } from './api'
import { ControlEmpty, ControlError, ControlLoading } from './components/ControlStates'
import { connectorStatusLabel, formatDateTime, PageIntro, StatusMark, statusTone } from './components/ControlPrimitives'
import { useControlRequest } from './hooks/useControlRequest'
import type { ConnectorKind } from './types'
import './control-center.css'

const kindMeta: Record<ConnectorKind, { label: string; icon: typeof Monitor }> = {
  host: { label: '读取主机', icon: Monitor },
  pbix: { label: 'Power BI 文件', icon: FileBarChart2 },
  bi_activity: { label: 'BI 使用记录', icon: History },
  directory: { label: '原始数据目录', icon: FolderSearch },
}

function permissionText(permission: string | undefined, readOnly: boolean) {
  if (permission === 'insufficient') return '读取权限不足'
  if (permission === 'read_only') return '操作系统只读'
  if (permission === 'application_read_only') return '应用按只读方式访问'
  return readOnly ? '只读访问' : '权限状态待确认'
}

export default function DataSourcesPage() {
  const state = useControlRequest(listConnectors)

  return <div className="cc-page">
    <PageIntro
      title="数据来源"
      description="系统只读取已允许的 finance-win 路径，不修改原文件，也不会主动下载尚未落地的 OneDrive 文件。"
      action={<button type="button" className="cc-button cc-button--secondary" onClick={state.reload} disabled={state.loading}><RefreshCw aria-hidden="true" />刷新状态</button>}
    />
    {state.loading ? <ControlLoading label="正在检查读取范围…" /> : null}
    {state.error ? <ControlError message={state.error} onRetry={state.reload} /> : null}
    {!state.loading && !state.error && state.data?.length === 0 ? <ControlEmpty title="尚未配置数据来源" description="管理员完成 finance-win 连接和允许路径设置后会显示在这里。" /> : null}
    {state.data && state.data.length > 0 ? <div className="cc-source-list">{state.data.map((connector) => {
      const meta = kindMeta[connector.kind]
      const Icon = meta.icon
      return <article className="cc-source-row" key={connector.id}>
        <span className="cc-source-icon"><Icon aria-hidden="true" /></span>
        <div className="cc-source-main">
          <div className="cc-source-title"><div><small>{meta.label}</small><h2>{connector.name}</h2></div><StatusMark tone={statusTone(connector.status)}>{connectorStatusLabel(connector.status)}</StatusMark></div>
          {connector.path ? <p className="cc-path" title={connector.path}>{connector.path}</p> : null}
          {connector.message ? <p className="cc-source-message">{connector.message}</p> : null}
          <dl className="cc-source-meta">
            <div><dt><ShieldCheck aria-hidden="true" />访问方式</dt><dd>{permissionText(connector.permission, connector.read_only)}</dd></div>
            <div><dt><Database aria-hidden="true" />最近扫描</dt><dd>{formatDateTime(connector.last_scan_at)}</dd></div>
            <div><dt>已发现</dt><dd>{connector.discovered_count ?? 0} 个</dd></div>
            <div><dt>尚未下载</dt><dd>{connector.offline_count ?? 0} 个</dd></div>
          </dl>
        </div>
      </article>
    })}</div> : null}
    <aside className="cc-note">
      <strong>读取边界</strong>
      <p>工资、汇总结果、学习资料、聊天数据库和系统目录默认排除。路径权限不足、文件尚未下载或文件仍在写入时，系统会跳过并明确说明。</p>
    </aside>
  </div>
}
