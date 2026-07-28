import { AlertCircle, Inbox, LoaderCircle, RefreshCw, WifiOff } from 'lucide-react'
import type { ReactNode } from 'react'
import '../control-center.css'

export function ControlLoading({ label = '正在加载…' }: { label?: string }) {
  return <div className="cc-state" role="status"><LoaderCircle className="cc-spin" aria-hidden="true" /><span>{label}</span></div>
}

export function ControlError({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return <div className="cc-state cc-state--error" role="alert"><AlertCircle aria-hidden="true" /><div><strong>暂时无法取得信息</strong><p>{message}</p>{onRetry ? <button type="button" className="cc-link-button" onClick={onRetry}><RefreshCw aria-hidden="true" />重新尝试</button> : null}</div></div>
}

export function ControlEmpty({ title, description, action }: { title: string; description: string; action?: ReactNode }) {
  return <div className="cc-state cc-state--empty"><Inbox aria-hidden="true" /><div><strong>{title}</strong><p>{description}</p>{action}</div></div>
}

export function ControlOffline({ title, description, action }: { title: string; description: string; action?: ReactNode }) {
  return <div className="cc-state cc-state--offline" role="status"><WifiOff aria-hidden="true" /><div><strong>{title}</strong><p>{description}</p>{action}</div></div>
}
