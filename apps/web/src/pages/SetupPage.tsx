import { FormEvent, useState } from 'react'
import { Navigate } from 'react-router-dom'
import { completeSetup } from '../api/auth'
import { Logo } from '../components/Logo'
import { useAuth } from '../context/AuthContext'

export default function SetupPage() {
  const auth = useAuth()
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  if (auth.status === 'authenticated') return <Navigate to="/admin/start" replace />
  if (auth.status === 'unauthenticated') return <Navigate to="/login" replace />

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const values = Object.fromEntries(new FormData(event.currentTarget))
    setBusy(true)
    setError('')
    try {
      await completeSetup({
        enterprise_name: String(values.enterprise_name),
        platform: String(values.platform),
        platform_account_name: String(values.platform_account_name),
        store_name: String(values.store_name),
        activation_at: String(values.activation_at),
        name: String(values.name),
        email: String(values.email),
        password: String(values.password),
      })
      await auth.refresh()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '初始化失败')
    } finally {
      setBusy(false)
    }
  }

  return <main className="auth-page">
    <section className="auth-card setup-card">
      <Logo />
      <h1>开始使用商析</h1>
      <p>创建首个企业和管理员。启用日期之前的数据不会进入正式经营结果。</p>
      <form onSubmit={submit}>
        <div className="form-grid"><label>企业名称<input name="enterprise_name" required /></label><label>销售平台<select name="platform" required><option value="">请选择</option><option value="tmall">天猫</option><option value="jd">京东</option><option value="douyin">抖音电商</option><option value="amazon">Amazon</option><option value="other">其他平台</option></select></label></div>
        <div className="form-grid"><label>平台账号名称<input name="platform_account_name" required /></label><label>店铺名称<input name="store_name" required /></label></div>
        <label>数据启用日期<input name="activation_at" type="date" required /></label>
        <div className="form-grid"><label>管理员姓名<input name="name" required /></label><label>管理员邮箱<input name="email" type="email" required autoComplete="username" /></label></div>
        <label>管理员密码<input name="password" type="password" minLength={12} required autoComplete="new-password" /><small>至少 12 个字符</small></label>
        {error ? <p className="form-error" role="alert">{error}</p> : null}
        <button className="button primary" disabled={busy}>{busy ? '正在创建…' : '创建并继续'}</button>
      </form>
    </section>
  </main>
}
