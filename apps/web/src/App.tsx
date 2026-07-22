import { lazy, Suspense } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import { LoadingState } from './components/AsyncState'
import AppShell from './components/AppShell'
import { RequireAdmin, RequireAuth, RequireUpload } from './components/RouteGuards'
import { AuthProvider } from './context/AuthContext'
import { FilterProvider } from './context/FilterContext'

const LoginPage = lazy(() => import('./pages/LoginPage'))
const SetupPage = lazy(() => import('./pages/SetupPage'))
const HomePage = lazy(() => import('./pages/HomePage'))
const UploadPage = lazy(() => import('./pages/UploadPage'))
const DashboardPage = lazy(() => import('./pages/DashboardPage'))
const AskPage = lazy(() => import('./pages/AskPage'))
const StartPage = lazy(() => import('./pages/admin/StartPage'))
const DataSettingsPage = lazy(() => import('./pages/admin/DataSettingsPage'))
const ProblemsPage = lazy(() => import('./pages/admin/ProblemsPage'))
const UsersPage = lazy(() => import('./pages/admin/UsersPage'))
const ReportsPage = lazy(() => import('./pages/admin/ReportsPage'))
const StatusPage = lazy(() => import('./pages/admin/StatusPage'))

export default function App() {
  return <AuthProvider><FilterProvider><Suspense fallback={<main className="center-screen"><LoadingState /></main>}><Routes>
    <Route path="/login" element={<LoginPage />} />
    <Route path="/setup" element={<SetupPage />} />
    <Route element={<RequireAuth />}>
      <Route element={<AppShell />}>
        <Route index element={<HomePage />} />
        <Route path="dashboard" element={<DashboardPage />} />
        <Route path="ask" element={<AskPage />} />
        <Route element={<RequireUpload />}><Route path="data" element={<UploadPage />} /></Route>
        <Route path="admin" element={<RequireAdmin />}>
          <Route index element={<Navigate to="start" replace />} />
          <Route path="start" element={<StartPage />} />
          <Route path="data" element={<DataSettingsPage />} />
          <Route path="problems" element={<ProblemsPage />} />
          <Route path="users" element={<UsersPage />} />
          <Route path="reports" element={<ReportsPage />} />
          <Route path="status" element={<StatusPage />} />
        </Route>
      </Route>
    </Route>
    <Route path="*" element={<Navigate to="/" replace />} />
  </Routes></Suspense></FilterProvider></AuthProvider>
}
