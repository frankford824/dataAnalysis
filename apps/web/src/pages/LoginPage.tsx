import { FormEvent, useState } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { Logo } from '../components/Logo'

export default function LoginPage() {
  const auth = useAuth()
  const location = useLocation()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  if (auth.status === 'authenticated') {
    const from = (location.state as { from?: string } | null)?.from || '/'
    return <Navigate to={from} replace />
  }
  if (auth.status === 'setup_required') return <Navigate to="/setup" replace />

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      await auth.signIn(email, password)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '登录失败')
    } finally {
      setBusy(false)
    }
  }

  return <main className="auth-page">
    <section className="auth-card">
      <Logo />
      <h1>登录经营数据平台</h1>
      <p>使用管理员分配的企业账号登录。</p>
      <form onSubmit={submit}>
        <label>邮箱<input autoComplete="username" type="email" required value={email} onChange={(event) => setEmail(event.target.value)} /></label>
        <label>密码<input autoComplete="current-password" type="password" required value={password} onChange={(event) => setPassword(event.target.value)} /></label>
        {error ? <p className="form-error" role="alert">{error}</p> : null}
        <button className="button primary" disabled={busy}>{busy ? '正在登录…' : '登录'}</button>
      </form>
    </section>
  </main>
}
