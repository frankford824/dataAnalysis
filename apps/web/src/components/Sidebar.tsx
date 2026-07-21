import { BarChart3, Bot, Boxes, Building2, Database, FileCheck2, FolderOpen, Gauge, HelpCircle, LayoutDashboard, MessageSquareText, PanelLeftClose, ShieldCheck, UploadCloud, Users } from 'lucide-react'
import { NavLink } from 'react-router-dom'
import { Logo } from './Logo'

const business = [
  ['/upload', UploadCloud, '本月数据'], ['/dashboard', LayoutDashboard, '经营看板'], ['/ask', MessageSquareText, '问业务'],
] as const
const admin = [
  ['/admin/organization', Users, '组织与店铺'], ['/admin/sources', Database, '数据来源'], ['/admin/models', Boxes, '模型与指标'], ['/admin/assets', FolderOpen, 'PBIX 资产'], ['/admin/ai', Bot, 'AI 设置'], ['/admin/publish', FileCheck2, '审核与发布'], ['/admin/reports', BarChart3, '报表设计'], ['/admin/users', Building2, '用户与权限'], ['/admin/audit', ShieldCheck, '审计与备份'],
] as const

export default function Sidebar({ open, onClose }: { open: boolean; onClose: () => void }) {
  const render = ([path, Icon, label]: typeof business[number] | typeof admin[number]) => <NavLink key={path} to={path} onClick={onClose} className={({ isActive }) => isActive ? 'nav-item active' : 'nav-item'}><Icon size={21}/><span>{label}</span></NavLink>
  return <><aside className={`sidebar ${open ? 'open' : ''}`}><Logo /><nav>{business.map(render)}<div className="nav-separator" /><span className="nav-heading">管理</span>{admin.map(render)}</nav><button className="collapse"><PanelLeftClose size={20}/> 收起导航</button></aside>{open && <button aria-label="关闭导航" className="scrim" onClick={onClose} />}</>
}
