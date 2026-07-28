import { lazy, Suspense } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import { LoadingState } from './components/AsyncState'
import AppShell from './components/AppShell'
import { RequireAuth, RequireDataConfig, RequireProblems, RequireUpload, RequireUserAdmin } from './components/RouteGuards'
import { AuthProvider } from './context/AuthContext'
import { FilterProvider } from './context/FilterContext'
import {
  DataSourcesPage,
  InboxPage,
  LlmBindingsPage,
  MorePage,
  OperationsPage,
  WorkCenterPage,
} from './features/control-center'

const LoginPage = lazy(() => import('./pages/LoginPage'))
const ChangePasswordPage = lazy(() => import('./pages/ChangePasswordPage'))
const SetupPage = lazy(() => import('./pages/SetupPage'))
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
      <Route path="change-password" element={<ChangePasswordPage />} />
      <Route element={<AppShell />}>
        <Route index element={<WorkCenterPage />} />
        <Route path="control/operations" element={<OperationsPage />} />
        <Route path="control/more" element={<MorePage />} />
        <Route path="dashboard" element={<DashboardPage />} />
        <Route path="ask" element={<AskPage />} />
        <Route element={<RequireUpload />}><Route path="data" element={<UploadPage />} /></Route>
        <Route element={<RequireDataConfig />}><Route path="control/data-sources" element={<DataSourcesPage />} /></Route>
        <Route element={<RequireProblems />}><Route path="control/inbox" element={<InboxPage />} /></Route>
        <Route element={<RequireUserAdmin />}><Route path="control/llm" element={<LlmBindingsPage />} /></Route>
        <Route path="admin" element={<Navigate to="/admin/start" replace />} />
        <Route element={<RequireDataConfig />}>
          <Route path="admin/start" element={<StartPage />} />
          <Route path="admin/data" element={<DataSettingsPage />} />
          <Route path="admin/status" element={<StatusPage />} />
        </Route>
        <Route element={<RequireProblems />}><Route path="admin/problems" element={<ProblemsPage />} /></Route>
        <Route element={<RequireUserAdmin />}>
          <Route path="admin/users" element={<UsersPage />} />
          <Route path="admin/reports" element={<ReportsPage />} />
        </Route>
      </Route>
    </Route>
    <Route path="*" element={<Navigate to="/" replace />} />
  </Routes></Suspense></FilterProvider></AuthProvider>
}
