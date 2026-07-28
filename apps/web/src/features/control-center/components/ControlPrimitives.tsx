import type { ReactNode } from 'react'
import type { ConnectorStatus, OperationStatus, ProgressConnectionState } from '../types'
import '../control-center.css'

const connectorLabels: Record<ConnectorStatus, string> = {
  not_configured: '尚未配置',
  connecting: '正在连接',
  ready: '可读取',
  permission_denied: '权限不足',
  offline: '主机离线',
  disabled: '已停用',
  scanning: '正在扫描',
  failed: '扫描失败',
}

const operationLabels: Record<OperationStatus, string> = {
  queued: '等待开始',
  waiting_for_worker: '等待外部执行器',
  scanning: '正在扫描',
  reading: '正在读取',
  processing: '正在确定性处理',
  waiting_for_review: '等待确认',
  resuming: '正在继续处理',
  completed: '已完成',
  failed: '失败',
}

const connectionLabels: Record<ProgressConnectionState, string> = {
  connecting: '正在连接实时进度',
  live: '实时更新',
  polling: '实时连接中断，正在定时刷新',
  offline: '暂时无法取得进度',
}

export function connectorStatusLabel(status: ConnectorStatus) {
  return connectorLabels[status]
}

export function operationStatusLabel(status: OperationStatus) {
  return operationLabels[status]
}

export function connectionStatusLabel(status: ProgressConnectionState) {
  return connectionLabels[status]
}

export function StatusMark({ tone, children }: { tone: 'neutral' | 'info' | 'success' | 'warning' | 'danger'; children: ReactNode }) {
  return <span className={`cc-status cc-status--${tone}`}>{children}</span>
}

export function statusTone(status: ConnectorStatus | OperationStatus): 'neutral' | 'info' | 'success' | 'warning' | 'danger' {
  if (status === 'ready' || status === 'completed') return 'success'
  if (status === 'connecting' || status === 'scanning' || status === 'reading' || status === 'processing' || status === 'resuming') return 'info'
  if (status === 'permission_denied' || status === 'failed') return 'danger'
  if (status === 'offline' || status === 'waiting_for_worker' || status === 'waiting_for_review') return 'warning'
  return 'neutral'
}

export function formatDateTime(value?: string) {
  if (!value) return '尚无记录'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date)
}

export function PageIntro({ title, description, action }: { title: string; description: string; action?: ReactNode }) {
  return <header className="cc-page-intro"><div><h1>{title}</h1><p>{description}</p></div>{action}</header>
}
