import { LogOut, Menu } from 'lucide-react'
import { useAuth } from '../context/AuthContext'

export default function Header({ onMenu }: { onMenu: () => void }) {
  const auth = useAuth()
  return <header className="topbar">
    <button aria-label="打开导航" className="menu-button" onClick={onMenu}><Menu /></button>
    <strong className="enterprise-name">{auth.user?.enterprise_name || '经营数据平台'}</strong>
    <div className="user-area">
      <span className="avatar">{auth.user?.name.slice(0, 1)}</span>
      <span className="user-name">{auth.user?.name}</span>
      <button className="logout-button" onClick={() => void auth.signOut()}><LogOut size={17} />退出</button>
    </div>
  </header>
}
