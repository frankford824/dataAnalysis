import { BarChart3, CircleHelp, Home, PlusSquare } from 'lucide-react'
import { NavLink } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'

export default function MobileNav() {
  const auth = useAuth()
  const links = [
    { to: '/', label: '首页', icon: Home, show: true },
    { to: '/data', label: '添加数据', icon: PlusSquare, show: auth.canUpload },
    { to: '/dashboard', label: '看板', icon: BarChart3, show: true },
    { to: '/ask', label: '问业务', icon: CircleHelp, show: true },
  ].filter((link) => link.show)
  return <nav className="mobile-nav" aria-label="移动端主导航">{links.map((link) => { const Icon = link.icon; return <NavLink key={link.to} to={link.to} end={link.to === '/'} className={({ isActive }) => isActive ? 'active' : ''}><Icon /><span>{link.label}</span></NavLink> })}</nav>
}
