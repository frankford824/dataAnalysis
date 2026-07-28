import { useState } from 'react'
import { Outlet } from 'react-router-dom'
import Header from './Header'
import Sidebar from './Sidebar'
import MobileNav from './MobileNav'

export default function AppShell() {
  const [open, setOpen] = useState(false)
  return <div className="app-shell">
    <a className="skip-link" href="#main-content">跳至主要内容</a>
    <Sidebar open={open} onClose={() => setOpen(false)} />
    <div className="main-column">
      <Header onMenu={() => setOpen(true)} />
      <main className="content" id="main-content" tabIndex={-1}><Outlet /></main>
      <MobileNav />
    </div>
  </div>
}
