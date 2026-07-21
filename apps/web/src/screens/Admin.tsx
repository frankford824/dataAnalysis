import { ArrowUpRight, Check, ChevronRight, CircleAlert, Plus, Search } from 'lucide-react'
import { FormEvent, useEffect, useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'
import { useApp } from '../context/AppContext'
import { adminDemo } from '../data/demo'
import type { ApiResource } from '../types'

const endpoint: Record<string,string> = { organization:'stores', sources:'sources', models:'semantic-models', assets:'model-assets', ai:'ai/providers', publish:'ingestions', reports:'dashboards', users:'users', audit:'audit-logs' }
const formFields: Record<string,{key:string;label:string;type?:string}[]> = {
  organization:[{key:'name',label:'店铺名称'},{key:'activation_at',label:'启用日期',type:'date'}],
  sources:[{key:'name',label:'数据来源名称'},{key:'coverage_time_field',label:'业务日期字段'},{key:'activation_at',label:'启用日期',type:'date'}],
  models:[{key:'name',label:'模型名称'}], assets:[{key:'name',label:'PBIX 文件名称'}], ai:[{key:'name',label:'服务商名称'},{key:'api_base',label:'API 地址'}], users:[{key:'name',label:'姓名'},{key:'email',label:'邮箱',type:'email'},{key:'role',label:'角色'}],
}

function resourceRows(section:string, resources:ApiResource[]):string[][] {
  if(section==='organization') return resources.map(r=>[r.name,'店铺',String(r.platform_account_id||'未指定'),String(r.status||'草稿'),String(r.activation_at||'—').slice(0,10)])
  if(section==='sources') return resources.map(r=>[r.name,String(r.arrival_frequency||'不定期'),String(r.scope_type||'企业范围'),'—',String(r.status||'草稿')])
  if(section==='models') return resources.map(r=>[r.name,String(r.industry_template||'自定义口径'),`v${r.version||1}`,String(r.status||'草稿'),String(r.effective_from||'—').slice(0,10)])
  if(section==='assets') return resources.map(r=>[r.name,String(r.scope_type||'企业范围'),String(r.input_contract?'已登记':'待登记'),String(r.validation_status||'等待检查'),`v${r.version||1}`])
  if(section==='ai') return resources.map(r=>[r.name,String(r.mode||'禁用'),'业务辅助','主备配置',String(r.status||'草稿')])
  if(section==='users') return resources.map(r=>[r.name,String(r.role||'viewer'),Array.isArray(r.store_ids)&&r.store_ids.length?`${r.store_ids.length} 家店铺`:'企业范围',String(r.status||'草稿'),String(r.updated_at||r.created_at||'—').slice(0,10)])
  return []
}

export default function Admin() {
  const { section='organization' }=useParams(); const config=adminDemo[section]||adminDemo.organization; const {api}=useApp(); const [resources,setResources]=useState<ApiResource[]>([]); const [live,setLive]=useState(false); const [search,setSearch]=useState(''); const [showForm,setShowForm]=useState(false); const [saving,setSaving]=useState(false); const [notice,setNotice]=useState('')
  useEffect(()=>{let active=true; const ep=endpoint[section]; if(!ep)return;api.list(ep).then(r=>{if(active){setResources(r);setLive(true)}}).catch(()=>{if(active){setResources([]);setLive(false)}});return()=>{active=false}},[api,section])
  const rows=useMemo(()=>{const source=live&&resources.length?resourceRows(section,resources):config.rows;return source.filter(row=>row.join(' ').toLowerCase().includes(search.toLowerCase()))},[live,resources,section,config.rows,search])
  const primary=()=>{if(section==='reports'){window.open((import.meta.env.VITE_SUPERSET_URL as string)||'/superset/','_blank','noopener');return}if(section==='publish'){setNotice('正在打开本月经营数据核对清单');return}if(section==='audit'){setNotice('健康检查已提交，结果会显示在本页');return}setShowForm(true)}
  const submit=async(e:FormEvent<HTMLFormElement>)=>{e.preventDefault();setSaving(true);const data=Object.fromEntries(new FormData(e.currentTarget));const extras:Record<string,unknown>={};if(section==='organization')Object.assign(extras,{status:'draft'});if(section==='sources')Object.assign(extras,{file_types:['xlsx','csv','zip'],data_granularity:'day',arrival_frequency:'adhoc',required:true,dedupe_keys:[],validations:[]});if(section==='models')Object.assign(extras,{industry_template:'ecommerce_standard',definition:{},quality_gates:[]});if(section==='assets')Object.assign(extras,{asset_type:'pbix',validation_status:'manual_required'});if(section==='ai')Object.assign(extras,{mode:'disabled'});if(section==='users')Object.assign(extras,{store_ids:[]});try{const created=await api.create(endpoint[section],{...data,...extras});setResources(v=>[created,...v]);setLive(true);setShowForm(false);setNotice('已保存为草稿，发布前仍需审核')}catch(err){setNotice(err instanceof Error?err.message:'保存失败，请稍后重试')}finally{setSaving(false)}}
  return <><div className="page-heading"><div><h1>{config.title}</h1><p>{config.description}</p></div><button className="button primary" onClick={primary}>{section==='reports'?<ArrowUpRight/>:<Plus/>}{config.action}</button></div>{notice&&<div className="notice"><Check/>{notice}<button onClick={()=>setNotice('')}>×</button></div>}<section className="admin-panel"><div className="admin-tools"><label className="search"><Search/><input aria-label="搜索" placeholder="搜索当前列表" value={search} onChange={e=>setSearch(e.target.value)}/></label><span className={live?'live-source':'demo-source'}>{live?'已连接服务':'服务未连接 · 示例内容'}</span></div><div className="table-scroll"><table><thead><tr>{config.columns.map(c=><th key={c}>{c}</th>)}<th aria-label="操作"/></tr></thead><tbody>{rows.map((row,i)=><tr key={i}>{row.map((cell,j)=><td key={j}>{j===3&&/等待|待|尚未/.test(cell)?<span className="warning-status"><CircleAlert/>{cell}</span>:cell}</td>)}<td><button className="row-action" aria-label="查看详情"><ChevronRight/></button></td></tr>)}</tbody></table></div><footer>共 {rows.length} 项</footer></section>{showForm&&<div className="modal-backdrop" onMouseDown={()=>setShowForm(false)}><section role="dialog" aria-modal="true" aria-labelledby="dialog-title" className="modal" onMouseDown={e=>e.stopPropagation()}><h2 id="dialog-title">{config.action}</h2><p>先保存为草稿，审核通过后才会影响正式经营结果。</p><form onSubmit={submit}>{(formFields[section]||[{key:'name',label:'名称'}]).map(f=><label key={f.key}>{f.label}<input name={f.key} type={f.type||'text'} required/></label>)}<div className="modal-actions"><button type="button" className="button secondary" onClick={()=>setShowForm(false)}>取消</button><button className="button primary" disabled={saving}>{saving?'正在保存…':'保存草稿'}</button></div></form></section></div>}</>
}
