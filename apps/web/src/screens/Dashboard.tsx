import { AlertTriangle, BarChart3, Download, Megaphone, MessageSquareText, RefreshCw, ShoppingBag, TrendingUp, WalletCards } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { dashboardDemo } from '../data/demo'
import { useApp } from '../context/AppContext'
import type { DashboardSummary } from '../types'

const money = new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY', maximumFractionDigits: 0 })

function TrendChart({ data }: { data: DashboardSummary['trend'] }) {
  const w = 820, h = 220, pad = 20, max = Math.max(1, ...data.flatMap(d => [d.revenue, d.profit])) * 1.08
  const x = (i: number) => data.length === 1 ? w / 2 : pad + i * ((w - pad * 2) / Math.max(1, data.length - 1))
  const point = (value: number, i: number) => `${x(i)},${h - 30 - value / max * (h - 60)}`
  const rev = data.map((d,i) => point(d.revenue,i)).join(' '), profit = data.map((d,i) => point(d.profit,i)).join(' ')
  return <div className="chart-wrap" aria-label="销售额与经营利润趋势"><svg viewBox={`0 0 ${w} ${h}`} role="img">
    {[0,1,2,3].map(i => <line key={i} x1="20" y1={25+i*48} x2="800" y2={25+i*48} stroke="#e7edf5" strokeDasharray="3 4"/>)}
    <polyline points={rev} fill="none" stroke="#1769ff" strokeWidth="3" strokeLinejoin="round"/><polyline points={profit} fill="none" stroke="#06a881" strokeWidth="3" strokeLinejoin="round"/>
    {data.map((d,i) => <g key={d.month}><circle cx={x(i)} cy={h - 30 - d.revenue / max * (h - 60)} r="3" fill="#fff" stroke="#1769ff" strokeWidth="2"/><text x={x(i)} y={h-5} textAnchor="middle" fontSize="10" fill="#667085">{d.month.slice(5)}</text></g>)}
  </svg></div>
}

export default function Dashboard() {
  const { api, filters, setOnline } = useApp(); const navigate = useNavigate(); const [data, setData] = useState(dashboardDemo); const [mode, setMode] = useState<'loading'|'live'|'demo'>('loading')
  useEffect(() => { let active = true; setMode('loading'); api.dashboard(filters).then(v => { if(active){ setData(v); setMode('live'); setOnline(true)}}).catch(() => { if(active){setData(dashboardDemo);setMode('demo');setOnline(false)}}); return () => {active=false} }, [api, filters, setOnline])
  const exportResult = async () => { try { const r = await api.export(filters); if (!r.ok) throw new Error(); const blob = await r.blob(); const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=`经营结果-${filters.period}.xlsx`;a.click();URL.revokeObjectURL(a.href) } catch { const csv='店铺,净销售额,退款,费用,经营利润\n'+data.stores.map(s=>[s.name,s.revenue,s.refund,s.fees,s.profit].join(',')).join('\n'); const a=document.createElement('a');a.href=URL.createObjectURL(new Blob(['\ufeff'+csv],{type:'text/csv'}));a.download=`经营结果-${filters.period}.csv`;a.click();URL.revokeObjectURL(a.href) } }
  const cards = [
    ['净销售额',data.revenue,'较上月 ↑ 18.6%',ShoppingBag,'blue'],['退款',data.refund,'较上月 ↑ 6.2%',RefreshCw,'red'],['平台与广告费用',data.fees,'较上月 ↓ 3.4%',Megaphone,'blue'],['经营利润',data.profit,'较上月 ↑ 24.1%',TrendingUp,'blue'],
  ] as const
  const liveCards = cards.map(([label,value,change,Icon,tone]) => [label,value,mode === 'live' ? '已确认' : change,Icon,tone] as const)
  const refundRate = data.revenue ? data.refund / data.revenue * 100 : 0, feeRate = data.revenue ? data.fees / data.revenue * 100 : 0, profitRate = data.revenue ? data.profit / data.revenue * 100 : 0
  const insights = mode === 'live' ? [
    [TrendingUp,'本月净销售额',`${filters.period} 已确认净销售额 ${money.format(data.revenue)}。`,'blue'],
    [Megaphone,'费用占比',`平台与广告费用占净销售额 ${feeRate.toFixed(2)}%。`,'green'],
    [WalletCards,'经营利润率',`已确认经营利润 ${money.format(data.profit)}，利润率 ${profitRate.toFixed(2)}%。`,'green'],
    [AlertTriangle,'退款率',`已确认退款 ${money.format(data.refund)}，退款率 ${refundRate.toFixed(2)}%。`,'red'],
  ] : [
    [TrendingUp,'净销售额创近 12 个月新高','6 月净销售额较上月增长 18.6%，主要得益于夏季新品和大促活动。','blue'],[Megaphone,'广告费用占比下降','平台与广告费用占净销售额 15.9%，投入效率有所提升。','green'],[WalletCards,'经营利润显著提升','经营利润较上月增长 24.1%，利润率达到 25.4%。','green'],[AlertTriangle,'退款率小幅上升','整体退款率为 5.23%，建议关注退货原因集中的商品。','red'],
  ]
  return <><div className="page-heading"><div><h1>经营看板</h1>{mode === 'demo' && <span className="demo-note">服务暂未连接，正在展示示例数据</span>}</div><div className="heading-actions">{mode === 'live' && <button className="button secondary" onClick={() => navigate('/reports')}><BarChart3 size={18}/>交互式报表</button>}<button className="button primary" onClick={() => navigate('/ask')}><MessageSquareText size={18}/>问一个业务问题</button><button className="button secondary" onClick={exportResult}><Download size={18}/>导出当前结果</button></div></div>
  <section className="metric-strip">{liveCards.map(([label,value,change,Icon,tone]) => <article className="metric" key={label}><span className={`metric-icon ${tone}`}><Icon/></span><div><span>{label}</span><strong>{money.format(value)}</strong><small className={tone==='red'?'danger':'success'}>{change}</small></div></article>)}</section>
  <div className="dashboard-grid"><div className="dashboard-main"><section className="panel chart-panel"><div className="panel-title"><div><h2>销售额与经营利润趋势</h2><div className="legend"><span className="rev"/>净销售额（元） <span className="profit"/>经营利润（元）</div></div><div className="period-switch"><button>日</button><button>周</button><button className="selected">月</button></div></div><TrendChart data={data.trend}/></section>
  <section className="panel table-panel"><h2>店铺经营表现</h2><div className="table-scroll"><table><thead><tr><th>店铺名称</th><th>净销售额（元）</th><th>较上月</th><th>退款（元）</th><th>退款率</th><th>平台与广告费用</th><th>经营利润</th><th>较上月</th></tr></thead><tbody>{data.stores.map(s=><tr key={s.id}><td>{s.name}</td><td>{s.revenue.toLocaleString()}</td><td className="success">{mode === 'live' ? '—' : `↑ ${s.change}%`}</td><td>{s.refund.toLocaleString()}</td><td>{s.refundRate}%</td><td>{s.fees.toLocaleString()}</td><td>{s.profit.toLocaleString()}</td><td className="success">{mode === 'live' ? '—' : `↑ ${s.profitChange}%`}</td></tr>)}</tbody></table></div><footer>共 {data.stores.length} 家店铺</footer></section></div>
  <aside className="panel insight-panel"><h2>值得关注</h2>{insights.map(([Icon,title,body,tone],i) => <article className="insight" key={i}><span className={`insight-icon ${tone}`}><Icon/></span><div><h3>{title as string}</h3><p>{body as string}</p></div></article>)}</aside></div></>
}
