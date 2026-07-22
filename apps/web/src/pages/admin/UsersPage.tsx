import { FormEvent, useState } from 'react'
import { inviteUser, listUsers, updateUser } from '../../api/admin'
import { listStores } from '../../api/resources'
import { EmptyState, ErrorState, LoadingState } from '../../components/AsyncState'
import PageHeader from '../../components/PageHeader'
import { useRequest } from '../../hooks/useRequest'
import type { Role, SessionUser, StoreResource } from '../../types'

const roles: Array<{ id: Role; label: string }> = [
  { id: 'viewer', label: '只看经营结果' },
  { id: 'analyst', label: '查看、问答和导出' },
  { id: 'implementer', label: '准备数据和处理问题' },
  { id: 'admin', label: '企业管理员' },
]

export default function UsersPage() {
  const state = useRequest(() => Promise.all([listUsers(), listStores()]), [])
  const [inviting, setInviting] = useState(false)
  const [temporaryPassword, setTemporaryPassword] = useState('')
  const [message, setMessage] = useState('')

  const changeRole = async (user: SessionUser, role: Role) => {
    setMessage('')
    try {
      await updateUser(user.id, { role })
      state.reload()
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : '权限更新失败')
    }
  }

  return <>
    <PageHeader title="用户权限" description="每位用户只能看到被分配的店铺；角色决定其可以执行的操作。" action={<button className="button primary" onClick={() => setInviting(true)}>邀请用户</button>} />
    {message ? <p className="notice" role="status">{message}</p> : null}
    {temporaryPassword ? <div className="temporary-password" role="status"><strong>一次性临时密码</strong><code>{temporaryPassword}</code>{navigator.clipboard ? <button className="text-button" onClick={() => void navigator.clipboard.writeText(temporaryPassword)}>复制</button> : null}<p>请通过安全渠道交给新用户；关闭后不会再次显示。</p><button className="button secondary" onClick={() => setTemporaryPassword('')}>我已保存</button></div> : null}
    {state.loading ? <LoadingState /> : null}
    {state.error ? <ErrorState message={state.error} retry={state.reload} /> : null}
    {state.data?.[0].length === 0 ? <EmptyState title="还没有用户" description="邀请首位业务用户开始使用。" /> : null}
    {state.data && state.data[0].length > 0 ? <section className="panel user-table"><div className="table-scroll"><table><thead><tr><th>姓名</th><th>邮箱</th><th>角色</th><th>店铺范围</th></tr></thead><tbody>{state.data[0].map((user) => <tr key={user.id}><td>{user.name}</td><td>{user.email}</td><td>{user.role === 'platform_admin' ? <span>平台管理员</span> : <select aria-label={`${user.name}的角色`} value={user.role} onChange={(event) => void changeRole(user, event.target.value as Role)}>{roles.map((role) => <option key={role.id} value={role.id}>{role.label}</option>)}</select>}</td><td>{user.store_ids?.length ? `${user.store_ids.length} 家店铺` : '全部有权店铺'}</td></tr>)}</tbody></table></div></section> : null}
    {inviting && state.data ? <InviteDialog stores={state.data[1]} close={() => setInviting(false)} completed={(password) => { setInviting(false); setTemporaryPassword(password); state.reload() }} /> : null}
  </>
}

function InviteDialog({ stores, close, completed }: { stores: StoreResource[]; close: () => void; completed: (password: string) => void }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const data = new FormData(event.currentTarget)
    setBusy(true)
    try {
      const result = await inviteUser({ name: String(data.get('name')), email: String(data.get('email')), role: String(data.get('role')), store_ids: data.getAll('store_ids').map(String) })
      completed(result.temporary_password)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '邀请失败')
    } finally {
      setBusy(false)
    }
  }
  return <div className="modal-backdrop" onMouseDown={close}><section className="modal" role="dialog" aria-modal="true" onMouseDown={(event) => event.stopPropagation()}><h2>邀请用户</h2><form onSubmit={submit}><label>姓名<input name="name" required /></label><label>邮箱<input name="email" type="email" required /></label><label>角色<select name="role">{roles.map((role) => <option key={role.id} value={role.id}>{role.label}</option>)}</select></label>{stores.length > 0 ? <fieldset><legend>可查看店铺（不选表示全部有权店铺）</legend>{stores.map((store) => <label key={store.id}><input type="checkbox" name="store_ids" value={store.id} />{store.name}</label>)}</fieldset> : null}{error ? <p className="form-error">{error}</p> : null}<div className="modal-actions"><button type="button" className="button secondary" onClick={close}>取消</button><button className="button primary" disabled={busy}>{busy ? '正在邀请…' : '发送邀请'}</button></div></form></section></div>
}
