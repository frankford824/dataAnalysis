import { Bell, Building2, CalendarDays, ChevronDown, Menu, Store } from 'lucide-react'
import { useApp } from '../context/AppContext'

function FilterSelect({ icon, value, onChange, options, label }: { icon: React.ReactNode; value: string; onChange: (v: string) => void; options: { id: string; name: string }[]; label: string }) {
  return <label className="filter-select"><span>{icon}</span><span className="sr-only">{label}</span><select value={value} onChange={e => onChange(e.target.value)}>{options.map(o => <option value={o.id} key={o.id}>{o.name}</option>)}</select><ChevronDown size={15}/></label>
}
export default function Header({ onMenu }: { onMenu: () => void }) {
  const { filters, setFilters, enterpriseOptions, platformOptions, storeOptions } = useApp()
  return <header className="topbar"><button aria-label="打开导航" className="menu-button" onClick={onMenu}><Menu /></button><div className="top-filters">
    <FilterSelect icon={<Building2 size={18}/>} label="企业" value={filters.enterpriseId} options={enterpriseOptions} onChange={enterpriseId => setFilters({ ...filters, enterpriseId, platformId: '', storeId: '' })}/>
    <FilterSelect icon={<Store size={18}/>} label="平台" value={filters.platformId} options={platformOptions} onChange={platformId => setFilters({ ...filters, platformId })}/>
    <FilterSelect icon={<Store size={18}/>} label="店铺" value={filters.storeId} options={storeOptions} onChange={storeId => setFilters({ ...filters, storeId })}/>
    <label className="filter-select"><CalendarDays size={18}/><span className="sr-only">月份</span><input aria-label="月份" type="month" value={filters.period} onChange={e => setFilters({ ...filters, period: e.target.value })}/></label>
  </div><div className="user-area"><Bell size={20}/><span className="avatar">张</span><span className="user-name">张小北</span><ChevronDown size={15}/></div></header>
}
