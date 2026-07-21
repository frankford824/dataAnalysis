import { useState, type ReactNode } from 'react'
import Header from './Header'
import Sidebar from './Sidebar'

export default function AppShell({ children }: { children: ReactNode }) { const [open, setOpen] = useState(false); return <div className="app-shell"><Sidebar open={open} onClose={() => setOpen(false)}/><div className="main-column"><Header onMenu={() => setOpen(true)}/><main className="content">{children}</main></div></div> }
