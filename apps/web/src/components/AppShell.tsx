import { useState } from 'react'
import { Outlet } from 'react-router-dom'
import Header from './Header'
import Sidebar from './Sidebar'
import MobileNav from './MobileNav'

export default function AppShell() {
  const [open, setOpen] = useState(false)
  return <div className="app-shell">
    <Sidebar open={open} onClose={() => setOpen(false)} />
    <div className="main-column">
      <Header onMenu={() => setOpen(true)} />
      <main className="content"><Outlet /></main>
      <MobileNav />
    </div>
  </div>
}
