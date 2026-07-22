import { useState } from 'react'
import { CheckCircle2, CircleAlert, RefreshCw } from 'lucide-react'
import { getDiagnostics } from '../../api/admin'
import { download } from '../../api/http'
import { ErrorState, LoadingState } from '../../components/AsyncState'
import PageHeader from '../../components/PageHeader'
import { useRequest } from '../../hooks/useRequest'

const labels: Record<string, string> = { database: '经营数据服务', object_storage: '文件存储', queue: '后台处理' }

export default function StatusPage() {
  const state = useRequest(getDiagnostics, [])
  const [backupError, setBackupError] = useState('')
  const backup = async () => {
    setBackupError('')
    try {
      await download('/configuration/export', `配置备份-${new Date().toISOString().slice(0, 10)}.json`)
    } catch (error) {
      setBackupError(error instanceof Error ? error.message : '导出失败')
    }
  }
  return <>
    <PageHeader title="系统状态" description="检查经营数据服务、文件存储和后台处理是否正常。" action={<div className="heading-actions"><button className="button secondary" onClick={backup}>导出配置备份</button><button className="button primary" onClick={state.reload}><RefreshCw size={17} />重新检查</button></div>} />
    {state.loading ? <LoadingState label="正在检查系统状态…" /> : null}
    {state.error ? <ErrorState message={state.error} retry={state.reload} /> : null}
    {backupError ? <p className="form-error" role="alert">{backupError}</p> : null}
    {state.data ? <section className="health-grid">{Object.entries(state.data.checks).map(([name, status]) => <article key={name} className={status === 'ok' ? 'healthy' : 'unhealthy'}>{status === 'ok' ? <CheckCircle2 /> : <CircleAlert />}<div><strong>{labels[name] || name}</strong><p>{status === 'ok' ? '运行正常' : '需要部署管理员处理'}</p></div></article>)}</section> : null}
  </>
}
