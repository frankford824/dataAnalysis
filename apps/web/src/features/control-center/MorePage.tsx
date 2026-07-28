import { Bot, ChevronRight, Database, FileCheck2, Settings, Users, type LucideIcon } from 'lucide-react'
import { useAuth } from '../../context/AuthContext'
import type { MoreLink } from './types'
import './control-center.css'

const defaultLinks: MoreLink[] = [
  { label: '数据来源', description: 'finance-win、PBIX 和原始目录', to: '/control/data-sources', permission: 'manage_connectors' },
  { label: '经营结果', description: '查看已认证结果和导出', to: '/dashboard', permission: 'view_results' },
  { label: 'LLM 配置', description: '运行方式、模型和任务绑定', to: '/control/llm', permission: 'manage_llm' },
  { label: '用户与权限', description: '账号、角色和可见范围', to: '/admin/users', permission: 'manage_users' },
  { label: '系统状态', description: '服务健康和运行诊断', to: '/admin/status', permission: 'view_system' },
]

const icons: Record<string, LucideIcon> = {
  '数据来源': Database,
  '经营结果': FileCheck2,
  'LLM 配置': Bot,
  '用户与权限': Users,
  '系统状态': Settings,
}

export default function MorePage({ permissions, links = defaultLinks }: { permissions?: string[]; links?: MoreLink[] }) {
  const auth = useAuth()
  const effectivePermissions = permissions ?? auth.capabilities
  const visible = links.filter((link) => !link.permission || effectivePermissions.includes(link.permission))
  return <div className="cc-page cc-more-page">
    <header className="cc-page-intro"><div><h1>更多</h1><p>按你的权限显示数据来源、经营结果和管理设置。</p></div></header>
    <nav className="cc-more-links" aria-label="更多功能">
      {visible.map((link) => {
        const Icon = icons[link.label] || Settings
        return <a href={link.to} key={link.to}><span className="cc-more-icon"><Icon aria-hidden="true" /></span><span><strong>{link.label}</strong><small>{link.description}</small></span><ChevronRight aria-hidden="true" /></a>
      })}
    </nav>
    {visible.length === 0 ? <p className="cc-muted">当前账号没有更多可用入口。</p> : null}
  </div>
}
