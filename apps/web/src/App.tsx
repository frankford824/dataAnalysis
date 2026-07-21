import { Navigate, Route, Routes } from 'react-router-dom'
import AppShell from './components/AppShell'
import { AppProvider } from './context/AppContext'
import Admin from './screens/Admin'
import SupersetReport from './screens/SupersetReport'
import AskBusiness from './screens/AskBusiness'
import Dashboard from './screens/Dashboard'
import Upload from './screens/Upload'

export default function App(){return <AppProvider><AppShell><Routes><Route path="/" element={<Navigate to="/dashboard" replace/>}/><Route path="/dashboard" element={<Dashboard/>}/><Route path="/reports" element={<SupersetReport/>}/><Route path="/upload" element={<Upload/>}/><Route path="/ask" element={<AskBusiness/>}/><Route path="/admin/:section" element={<Admin/>}/><Route path="*" element={<Navigate to="/dashboard" replace/>}/></Routes></AppShell></AppProvider>}
