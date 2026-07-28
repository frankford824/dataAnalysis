import { Navigate, Outlet, useLocation } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { LoadingState } from './AsyncState'

export function RequireAuth() {
  const auth = useAuth()
  const location = useLocation()
  if (auth.status === 'loading') return <main className="center-screen"><LoadingState label="正在确认登录状态…" /></main>
  if (auth.status === 'setup_required') return <Navigate to="/setup" replace />
  if (auth.status !== 'authenticated') return <Navigate to="/login" replace state={{ from: location.pathname }} />
  if (auth.user?.must_change_password && location.pathname !== '/change-password') return <Navigate to="/change-password" replace />
  return <Outlet />
}

export function RequireUserAdmin() {
  const auth = useAuth()
  return auth.canManageUsers ? <Outlet /> : <Navigate to="/" replace />
}

export function RequireDataConfig() {
  const auth = useAuth()
  return auth.canConfigureData ? <Outlet /> : <Navigate to="/" replace />
}

export function RequireProblems() {
  const auth = useAuth()
  return auth.canResolveProblems ? <Outlet /> : <Navigate to="/" replace />
}

export function RequireUpload() {
  const auth = useAuth()
  return auth.canUpload ? <Outlet /> : <Navigate to="/" replace />
}
