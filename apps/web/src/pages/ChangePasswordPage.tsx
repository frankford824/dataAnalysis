import { FormEvent, useState } from 'react'
import { Navigate, useNavigate } from 'react-router-dom'
import { changePassword } from '../api/auth'
import { Logo } from '../components/Logo'
import { useAuth } from '../context/AuthContext'

export default function ChangePasswordPage() {
  const auth = useAuth()
  const navigate = useNavigate()
  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  if (auth.status === 'authenticated' && !auth.user?.must_change_password) return <Navigate to="/" replace />

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (newPassword !== confirmation) {
      setError('两次输入的新密码不一致')
      return
    }
    setBusy(true)
    setError('')
    try {
      await changePassword(currentPassword, newPassword)
      await auth.refresh()
      navigate('/', { replace: true })
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '修改密码失败')
    } finally {
      setBusy(false)
    }
  }

  return <main className="auth-page password-change-page">
    <section className="auth-card password-change-card">
      <Logo />
      <p className="eyebrow">首次登录安全检查</p>
      <h1>设置您自己的密码</h1>
      <p>临时密码只能用于本次登录。完成修改后，其他登录会话将失效。</p>
      <form onSubmit={submit}>
        <label>当前临时密码<input autoComplete="current-password" type="password" required value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} /></label>
        <label>新密码<input autoComplete="new-password" type="password" minLength={12} required value={newPassword} onChange={(event) => setNewPassword(event.target.value)} /></label>
        <label>再次输入新密码<input autoComplete="new-password" type="password" minLength={12} required value={confirmation} onChange={(event) => setConfirmation(event.target.value)} /></label>
        <p className="field-hint">至少 12 位，建议同时包含大小写字母、数字和符号。</p>
        {error ? <p className="form-error" role="alert">{error}</p> : null}
        <button className="button primary" disabled={busy}>{busy ? '正在保存…' : '保存新密码并继续'}</button>
      </form>
    </section>
  </main>
}
