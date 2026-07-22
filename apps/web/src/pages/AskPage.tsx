import { useState } from 'react'
import { askBusiness } from '../api/analytics'
import { ErrorState } from '../components/AsyncState'
import BusinessFilters from '../components/BusinessFilters'
import PageHeader from '../components/PageHeader'
import { useFilters } from '../context/FilterContext'

const questionTypes = [
  { id: 'sales', label: '本月销售', question: '当前范围的本月净销售额是多少？' },
  { id: 'refund', label: '本月退款', question: '当前范围的本月退款金额是多少？' },
  { id: 'fees', label: '本月费用', question: '当前范围的本月费用是多少？' },
  { id: 'profit', label: '本月利润', question: '当前范围的本月经营利润是多少？' },
  { id: 'ranking', label: '店铺排名', question: '当前范围按经营利润进行店铺排名。' },
  { id: 'month_comparison', label: '上月比较', question: '当前范围净销售额与上月相比有什么变化？' },
  { id: 'refund_rate', label: '退款率', question: '当前范围的退款率是多少？' },
  { id: 'profit_margin', label: '利润率', question: '当前范围的利润率是多少？' },
]

export default function AskPage() {
  const { filters } = useFilters()
  const [answer, setAnswer] = useState('')
  const [selected, setSelected] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const ask = async (type: typeof questionTypes[number]) => {
    setSelected(type.id)
    setBusy(true)
    setError('')
    setAnswer('')
    try {
      const result = await askBusiness(type.question, filters, type.id)
      setAnswer(result.answer)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '问题没有得到回答')
    } finally {
      setBusy(false)
    }
  }

  return <>
    <PageHeader title="问业务" description="选择一类问题，回答只使用您有权查看的已发布数据。" />
    <BusinessFilters />
    <section className="question-types" aria-label="问题类型">
      {questionTypes.map((type) => <button key={type.id} className={selected === type.id ? 'selected' : ''} onClick={() => void ask(type)} disabled={busy}><strong>{type.label}</strong><span>{type.question}</span></button>)}
    </section>
    {busy ? <div className="answer-panel" role="status">正在核对经营数据…</div> : null}
    {error ? <ErrorState message={error} /> : null}
    {answer ? <section className="answer-panel" aria-live="polite"><h2>回答</h2><p>{answer}</p><small>范围：{filters.dateFrom} 至 {filters.dateTo}</small></section> : null}
  </>
}
