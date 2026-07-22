import {
  BarChart3,
  CircleHelp,
  Database,
  Gauge,
  Home,
  LayoutTemplate,
  PanelLeftClose,
  ShieldCheck,
  UploadCloud,
  Users,
} from 'lucide-react'
import { NavLink } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { Logo } from './Logo'

const userLinks = [
  { to: '/', icon: Home, label: '首页', end: true },
  { to: '/data', icon: UploadCloud, label: '添加本月数据', upload: true },
  { to: '/dashboard', icon: BarChart3, label: '经营看板' },
  { to: '/ask', icon: CircleHelp, label: '问业务' },
]

const adminLinks = [
  { to: '/admin/start', icon: LayoutTemplate, label: '开始使用' },
  { to: '/admin/data', icon: Database, label: '数据设置' },
  { to: '/admin/problems', icon: CircleHelp, label: '待处理问题' },
  { to: '/admin/users', icon: Users, label: '用户权限' },
  { to: '/admin/reports', icon: Gauge, label: '高级报表设计' },
  { to: '/admin/status', icon: ShieldCheck, label: '系统状态' },
]

export default function Sidebar({ open, onClose }: { open: boolean; onClose: () => void }) {
  const auth = useAuth()
  const links = userLinks.filter((link) => !link.upload || auth.canUpload)
  const renderLink = (link: typeof userLinks[number]) => {
    const Icon = link.icon
    return <NavLink key={link.to} to={link.to} end={'end' in link && link.end} onClick={onClose} className={({ isActive }) => isActive ? 'nav-item active' : 'nav-item'}><Icon size={21} /><span>{link.label}</span></NavLink>
  }
  return <>
    <aside className={`sidebar ${open ? 'open' : ''}`}>
      <Logo />
      <nav aria-label="主导航">
        {links.map(renderLink)}
        {auth.canManage ? <><div className="nav-separator" /><span className="nav-heading">管理</span>{adminLinks.map(renderLink)}</> : null}
      </nav>
      <button className="collapse" onClick={onClose}><PanelLeftClose size={20} />收起导航</button>
    </aside>
    {open ? <button aria-label="关闭导航" className="scrim" onClick={onClose} /> : null}
  </>
}
