import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { getSession, login as loginRequest, logout as logoutRequest, setupStatus } from '../api/auth'
import type { Role, SessionUser } from '../types'

type AuthStatus = 'loading' | 'setup_required' | 'unauthenticated' | 'authenticated'
type AuthValue = {
  status: AuthStatus
  user: SessionUser | null
  signIn(email: string, password: string): Promise<void>
  signOut(): Promise<void>
  refresh(): Promise<void>
  canManage: boolean
  canUpload: boolean
}

const AuthContext = createContext<AuthValue | null>(null)
const MANAGEMENT_ROLES = new Set<Role>(['platform_admin', 'admin', 'implementer'])

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>('loading')
  const [user, setUser] = useState<SessionUser | null>(null)

  const refresh = useCallback(async () => {
    setStatus('loading')
    const [setup, session] = await Promise.allSettled([setupStatus(), getSession()])
    if (setup.status === 'fulfilled' && !setup.value.initialized) {
      setUser(null)
      setStatus('setup_required')
      return
    }
    if (session.status === 'fulfilled') {
      setUser(session.value)
      setStatus('authenticated')
      return
    }
    setUser(null)
    setStatus('unauthenticated')
  }, [])

  useEffect(() => { void refresh() }, [refresh])
  useEffect(() => {
    const expired = () => { setUser(null); setStatus('unauthenticated') }
    window.addEventListener('auth-expired', expired)
    return () => window.removeEventListener('auth-expired', expired)
  }, [])

  const signIn = useCallback(async (email: string, password: string) => {
    const next = await loginRequest(email, password)
    setUser(next)
    setStatus('authenticated')
  }, [])

  const signOut = useCallback(async () => {
    await logoutRequest()
    setUser(null)
    setStatus('unauthenticated')
  }, [])

  const value = useMemo<AuthValue>(() => ({
    status,
    user,
    signIn,
    signOut,
    refresh,
    canManage: Boolean(user && MANAGEMENT_ROLES.has(user.role)),
    canUpload: Boolean(user && MANAGEMENT_ROLES.has(user.role)),
  }), [status, user, signIn, signOut, refresh])

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const value = useContext(AuthContext)
  if (!value) throw new Error('AuthProvider missing')
  return value
}
