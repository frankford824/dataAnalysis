import { CircleHelp, Home, Menu, TimerReset } from 'lucide-react'
import { NavLink } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'

export default function MobileNav() {
  const auth = useAuth()
  const links = [
    { to: '/', label: '工作台', icon: Home, show: true },
    { to: '/control/operations', label: '进度', icon: TimerReset, show: auth.canViewOperations },
    { to: '/control/inbox', label: '收件箱', icon: CircleHelp, show: auth.canResolveProblems },
    { to: '/control/more', label: '更多', icon: Menu, show: true },
  ].filter((link) => link.show)
  return <nav className="mobile-nav" aria-label="移动端主导航">
    {links.map((link) => {
      const Icon = link.icon
      return <NavLink key={link.to} to={link.to} end={link.to === '/'} className={({ isActive }) => isActive ? 'active' : ''}>
        <Icon aria-hidden="true" />
        <span>{link.label}</span>
      </NavLink>
    })}
  </nav>
}
