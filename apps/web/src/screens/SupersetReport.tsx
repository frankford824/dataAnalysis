import { embedDashboard } from '@superset-ui/embedded-sdk'
import { CircleAlert, LoaderCircle } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { useApp } from '../context/AppContext'
import type { ApiResource } from '../types'

export default function SupersetReport() {
  const { api } = useApp()
  const mount = useRef<HTMLDivElement>(null)
  const [state, setState] = useState<'loading'|'ready'|'failed'>('loading')
  useEffect(() => {
    let active = true
    let cleanup: (() => void) | undefined
    async function start() {
      try {
        const dashboards = await api.list<ApiResource>('dashboards')
        const dashboard = dashboards.find(item => item.bi_adapter === 'superset' && item.external_id)
        if (!dashboard || !mount.current) throw new Error('dashboard unavailable')
        const token = await api.dashboardEmbedToken(dashboard.id)
        const embedded = await embedDashboard({
          id: token.embedded_id,
          supersetDomain: (import.meta.env.VITE_SUPERSET_URL as string) || 'http://localhost:8088',
          mountPoint: mount.current,
          fetchGuestToken: async () => (await api.dashboardEmbedToken(dashboard.id)).token,
          dashboardUiConfig: { hideTitle: true, hideChartControls: true, filters: { expanded: false } },
        })
        cleanup = () => embedded.unmount()
        if (active) setState('ready')
      } catch { if (active) setState('failed') }
    }
    void start()
    return () => { active = false; cleanup?.() }
  }, [api])
  return <><div className="page-heading"><div><h1>交互式经营报表</h1><p>只展示已核对发布的数据，筛选范围受当前企业权限约束。</p></div></div><section className="panel embedded-report"><div ref={mount}/>{state === 'loading' && <div className="embed-state"><LoaderCircle className="spin"/>正在加载报表…</div>}{state === 'failed' && <div className="embed-state error"><CircleAlert/>报表暂时不可用，请联系实施人员。</div>}</section></>
}
