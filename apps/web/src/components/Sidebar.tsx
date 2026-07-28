import {
  BarChart3,
  Bot,
  CircleHelp,
  Database,
  Home,
  PanelLeftClose,
  ShieldCheck,
  TimerReset,
  Users,
} from 'lucide-react'
import { NavLink } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { Logo } from './Logo'

type NavItem = {
  to: string
  icon: typeof Home
  label: string
  end?: boolean
  show?: boolean
}

function NavigationLink({ item, onClose }: { item: NavItem; onClose: () => void }) {
  const Icon = item.icon
  return <NavLink
    to={item.to}
    end={item.end}
    onClick={onClose}
    className={({ isActive }) => isActive ? 'nav-item active' : 'nav-item'}
  >
    <Icon size={21} aria-hidden="true" />
    <span>{item.label}</span>
  </NavLink>
}

export default function Sidebar({ open, onClose }: { open: boolean; onClose: () => void }) {
  const auth = useAuth()
  const primary: NavItem[] = [
    { to: '/', icon: Home, label: '工作台', end: true },
    { to: '/control/data-sources', icon: Database, label: '数据来源', show: auth.canManageConnectors },
    { to: '/control/operations', icon: TimerReset, label: '处理进度', show: auth.canViewOperations },
    { to: '/control/inbox', icon: CircleHelp, label: '确认收件箱', show: auth.canResolveProblems },
    { to: '/dashboard', icon: BarChart3, label: '经营结果', show: auth.canViewResults },
  ].filter((item) => item.show !== false)
  const management: NavItem[] = [
    { to: '/control/llm', icon: Bot, label: 'LLM 配置', show: auth.canManageLlm },
    { to: '/admin/users', icon: Users, label: '用户与权限', show: auth.canManageUsers },
    { to: '/admin/status', icon: ShieldCheck, label: '系统状态', show: auth.canViewSystem },
  ].filter((item) => item.show !== false)

  return <>
    <aside className={`sidebar ${open ? 'open' : ''}`}>
      <Logo />
      <nav aria-label="主导航">
        {primary.map((item) => <NavigationLink key={item.to} item={item} onClose={onClose} />)}
        {management.length > 0 ? <>
          <div className="nav-separator" />
          <span className="nav-heading">管理</span>
          {management.map((item) => <NavigationLink key={item.to} item={item} onClose={onClose} />)}
        </> : null}
      </nav>
      <button className="collapse" onClick={onClose}><PanelLeftClose size={20} aria-hidden="true" />收起导航</button>
    </aside>
    {open ? <button aria-label="关闭导航" className="scrim" onClick={onClose} /> : null}
  </>
}
