import type { ReactNode } from 'react'

export default function PageHeader({ title, description, action }: { title: string; description?: string; action?: ReactNode }) {
  return <div className="page-heading"><div><h1>{title}</h1>{description ? <p>{description}</p> : null}</div>{action}</div>
}
