import { CircleAlert, Inbox, LoaderCircle } from 'lucide-react'
import type { ReactNode } from 'react'

export function LoadingState({ label = '正在加载…' }: { label?: string }) {
  return <div className="state-box" role="status"><LoaderCircle className="spin" /><span>{label}</span></div>
}

export function ErrorState({ message, retry }: { message: string; retry?: () => void }) {
  return <div className="state-box error" role="alert"><CircleAlert /><div><strong>暂时无法加载</strong><p>{message}</p>{retry ? <button className="text-button" onClick={retry}>重新尝试</button> : null}</div></div>
}

export function EmptyState({ title, description, action }: { title: string; description: string; action?: ReactNode }) {
  return <div className="state-box empty"><Inbox /><div><strong>{title}</strong><p>{description}</p>{action}</div></div>
}
