import { useEffect, useState } from 'react'
import { fetchHome, type HomeBar, type HomeResponse } from './api'
import './home.css'

type Props = {
  onOpenDecide: () => void
  onOpenFiles: () => void
  onOpenRecoverable: () => void
  onOpenConclusion: () => void
}

function barAction(
  bar: HomeBar,
  handlers: Props,
): (() => void) | undefined {
  if (!bar.action) return undefined
  if (bar.id === 'decide') return handlers.onOpenDecide
  if (bar.id === 'files') return handlers.onOpenFiles
  if (bar.id === 'recoverable') return handlers.onOpenRecoverable
  if (bar.id === 'conclusion') return handlers.onOpenConclusion
  return undefined
}

export function HomeScreen(props: Props) {
  const [data, setData] = useState<HomeResponse | null>(null)
  const [error, setError] = useState('')
  const [showNotes, setShowNotes] = useState(false)

  useEffect(() => {
    let cancelled = false
    fetchHome()
      .then((next) => {
        if (!cancelled) setData(next)
      })
      .catch((reason: unknown) => {
        if (!cancelled) {
          setError(reason instanceof Error ? reason.message : '加载失败')
        }
      })
    return () => {
      cancelled = true
    }
  }, [])

  if (error) {
    return <div className="home-page"><p className="home-error">{error}</p></div>
  }
  if (!data) {
    return <div className="home-page"><p className="home-loading">正在看这个月做到哪一步…</p></div>
  }

  return (
    <div className="home-page">
      <header className="home-header">
        <div>
          <h1>这个月</h1>
          <p>按做到哪一步看，不用在系统里找菜单。</p>
        </div>
        <button
          type="button"
          className="home-bell"
          onClick={() => setShowNotes((value) => !value)}
          aria-label="站内提醒"
        >
          提醒
          {data.notifications.length > 0 ? (
            <span className="home-badge">{data.notifications.length}</span>
          ) : null}
        </button>
      </header>

      {showNotes ? (
        <section className="home-notes" aria-label="站内提醒">
          {data.notifications.length === 0 ? (
            <p>暂时没有新提醒。</p>
          ) : (
            data.notifications.map((note) => (
              <article key={note.id}>
                <strong>{note.title}</strong>
                <p>{note.body}</p>
              </article>
            ))
          )}
        </section>
      ) : null}

      <section className="home-bars" aria-label="本月进度">
        {data.bars.map((bar) => {
          const onClick = barAction(bar, props)
          return (
            <article
              key={bar.id}
              className={`home-bar${bar.active ? ' is-active' : ''}`}
            >
              <div>
                <h2>{bar.title}</h2>
                <p>{bar.summary}</p>
              </div>
              {bar.action && onClick ? (
                <button type="button" className="home-bar-action" onClick={onClick}>
                  {bar.action}
                </button>
              ) : null}
            </article>
          )
        })}
      </section>
    </div>
  )
}
