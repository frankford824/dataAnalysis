import { useEffect, useState } from 'react'

export type DecisionOption = {
  code: string
  label: string
  recommended: boolean
}

export type DecisionCard = {
  questionId: string
  what: string
  why: string
  options: DecisionOption[]
  index: number
  total: number
  consequence?: {
    summary: string
    booksSafe: boolean
    details: string[]
  } | null
}

type Props = {
  card: DecisionCard | null
  loading: boolean
  onSelect: (code: string) => void
  onConfirm: () => void
  onBack: () => void
  selectedCode: string
  busy: boolean
}

export function DecisionFlow({
  card,
  loading,
  onSelect,
  onConfirm,
  onBack,
  selectedCode,
  busy,
}: Props) {
  if (loading) {
    return <div className="decision-page"><p>正在准备下一件事…</p></div>
  }
  if (!card) {
    return (
      <div className="decision-page">
        <p>目前没有需要你定的事。</p>
        <button type="button" onClick={onBack}>回主页</button>
      </div>
    )
  }

  return (
    <div className="decision-page">
      <header className="decision-header">
        <button type="button" onClick={onBack}>返回</button>
        <span>第 {card.index} 件 / 共 {card.total} 件</span>
      </header>
      <section>
        <h1>是什么</h1>
        <p>{card.what}</p>
      </section>
      <section>
        <h2>为什么问你</h2>
        <p>{card.why}</p>
      </section>
      <fieldset disabled={busy}>
        <legend>怎么选</legend>
        {card.options.map((option) => (
          <label key={option.code} className="decision-option">
            <input
              type="radio"
              name="decision"
              value={option.code}
              checked={selectedCode === option.code}
              onChange={() => onSelect(option.code)}
            />
            <span>
              {option.label}
              {option.recommended ? '（推荐）' : ''}
            </span>
          </label>
        ))}
      </fieldset>
      {card.consequence ? (
        <section className={card.consequence.booksSafe ? 'is-safe' : 'is-risk'}>
          <h2>选了会怎样</h2>
          <p>{card.consequence.summary}</p>
          <ul>
            {card.consequence.details.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </section>
      ) : null}
      <button
        type="button"
        className="decision-confirm"
        disabled={!selectedCode || busy}
        onClick={onConfirm}
      >
        {busy ? '正在确认…' : '就这样定'}
      </button>
    </div>
  )
}

export function useEscapeToBack(onBack: () => void) {
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onBack()
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [onBack])
}

export function useSelectedCode(initial = '') {
  const [selectedCode, setSelectedCode] = useState(initial)
  return { selectedCode, setSelectedCode }
}
