import { Navigate, Outlet, useLocation } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { LoadingState } from './AsyncState'

export function RequireAuth() {
  const auth = useAuth()
  const location = useLocation()
  if (auth.status === 'loading') return <main className="center-screen"><LoadingState label="正在确认登录状态…" /></main>
  if (auth.status === 'setup_required') return <Navigate to="/setup" replace />
  if (auth.status !== 'authenticated') return <Navigate to="/login" replace state={{ from: location.pathname }} />
  return <Outlet />
}

export function RequireAdmin() {
  const auth = useAuth()
  return auth.canManage ? <Outlet /> : <Navigate to="/" replace />
}

export function RequireUpload() {
  const auth = useAuth()
  return auth.canUpload ? <Outlet /> : <Navigate to="/" replace />
}
