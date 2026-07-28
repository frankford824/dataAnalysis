import { FormEvent, useState, type ReactNode } from 'react'
import { Building2, Database, Pencil, Store as StoreIcon } from 'lucide-react'
import { createResource, listPlatforms, listSources, listStores, updateResource } from '../../api/resources'
import { EmptyState, ErrorState, LoadingState } from '../../components/AsyncState'
import PageHeader from '../../components/PageHeader'
import { useRequest } from '../../hooks/useRequest'
import type { PlatformResource, SourceResource, StoreResource } from '../../types'

type Tab = 'platforms' | 'stores' | 'sources'
type Editable = PlatformResource | StoreResource | SourceResource
type EditorState = { kind: 'platform' | 'store' | 'source'; item?: Editable }

export default function DataSettingsPage() {
  const state = useRequest(() => Promise.all([listPlatforms(), listStores(), listSources()]), [])
  const [tab, setTab] = useState<Tab>('platforms')
  const [editor, setEditor] = useState<EditorState | null>(null)
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const platforms = state.data?.[0] ?? []
  const stores = state.data?.[1] ?? []
  const sources = state.data?.[2] ?? []

  const openCreate = () => setEditor({ kind: tab === 'platforms' ? 'platform' : tab === 'stores' ? 'store' : 'source' })
  const switchTab = (next: Tab) => { setTab(next); setEditor(null); setError('') }
  const saved = (text: string) => { setEditor(null); setError(''); setMessage(text); state.reload() }

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!editor) return
    const data = new FormData(event.currentTarget)
    setBusy(true)
    setError('')
    try {
      let payload: Record<string, unknown>
      if (editor.kind === 'platform') {
        payload = {
          name: required(data, 'name'), platform: required(data, 'platform'),
          external_account_id: optional(data, 'external_account_id'), status: 'active',
        }
      } else if (editor.kind === 'store') {
        payload = {
          name: required(data, 'name'), platform_account_id: required(data, 'platform_account_id'),
          external_store_id: optional(data, 'external_store_id'),
          activation_at: `${required(data, 'activation_at')}T00:00:00Z`, status: 'active',
        }
      } else {
        const aliases = {
          occurred_at: csv(data, 'date_aliases'), order_id: csv(data, 'order_aliases'), store_id: csv(data, 'store_aliases'),
          revenue: csv(data, 'sales_aliases'), refund: csv(data, 'refund_aliases'), platform_fee: csv(data, 'platform_fee_aliases'),
          advertising_fee: csv(data, 'advertising_fee_aliases'), shipping_fee: csv(data, 'shipping_fee_aliases'), product_cost: csv(data, 'cost_aliases'),
        }
        const requiredFields = data.getAll('required_fields').map(String)
        const validations: Array<Record<string, unknown>> = requiredFields.map((field) => ({ type: 'required_field', field }))
        const dependency = String(data.get('dependency_source_logical_id') || '')
        if (dependency) validations.push({ type: 'cross_source_match', mode: 'required_source', dependency_source_logical_id: dependency, label: sources.find((source) => source.logical_id === dependency)?.name || '必需文件' })
        const dedupeStrategy = required(data, 'dedupe_strategy')
        payload = {
          name: required(data, 'name'), activation_at: `${required(data, 'activation_at')}T00:00:00Z`,
          coverage_time_field: 'occurred_at', store_field: aliases.store_id[0] || undefined,
          data_granularity: required(data, 'data_granularity'), arrival_frequency: required(data, 'arrival_frequency'),
          import_mode: required(data, 'import_mode'), source_kind: required(data, 'source_kind'),
          file_types: data.getAll('file_types').map(String), field_aliases: aliases,
          recognition: { required_headers: requiredFields },
          dedupe_keys: dedupeStrategy === 'order' ? ['order_id'] : ['event_type', 'occurred_at', 'store_id'],
          amount_directions: Object.fromEntries(data.getAll('negative_amount_fields').map((field) => [String(field), 'negative'])),
          validations, required: data.get('required') === 'on', status: 'active',
        }
      }
      const resource = editor.kind === 'platform' ? 'platforms' : editor.kind === 'store' ? 'stores' : 'sources'
      if (editor.item) await updateResource(resource, editor.item.id, payload)
      else await createResource(resource, payload)
      saved(editor.item ? '修改已按生效时间保存；历史月份仍使用原版本。' : '已新增并启用。')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '保存失败')
    } finally { setBusy(false) }
  }

  const setStatus = async (resource: Tab, item: Editable) => {
    setMessage('')
    try {
      const next = item.status === 'active' ? 'archived' : 'active'
      await updateResource(resource, item.id, { status: next })
      setMessage(next === 'active' ? '已启用。' : '已停用；历史关系和已发布数据仍保留。')
      state.reload()
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : '更新失败') }
  }

  return <>
    <PageHeader title="数据设置" description="维护平台账号、店铺归属和每月需要添加的数据内容。" action={<button className="button primary" onClick={openCreate}>{tab === 'platforms' ? '新增平台账号' : tab === 'stores' ? '新增店铺' : '新增数据内容'}</button>} />
    <div className="settings-tabs" role="tablist" aria-label="数据设置分类">
      <TabButton active={tab === 'platforms'} onClick={() => switchTab('platforms')} icon={<Building2 />} label="平台账号" count={platforms.length} />
      <TabButton active={tab === 'stores'} onClick={() => switchTab('stores')} icon={<StoreIcon />} label="店铺" count={stores.length} />
      <TabButton active={tab === 'sources'} onClick={() => switchTab('sources')} icon={<Database />} label="数据内容" count={sources.length} />
    </div>
    {message ? <p className="notice" role="status">{message}</p> : null}
    {editor ? <Editor editor={editor} platforms={platforms} sources={sources} submit={submit} close={() => setEditor(null)} busy={busy} error={error} /> : null}
    {state.loading ? <LoadingState /> : null}
    {state.error ? <ErrorState message={state.error} retry={state.reload} /> : null}
    {state.data && tab === 'platforms' ? <PlatformList items={platforms} edit={(item) => setEditor({ kind: 'platform', item })} toggle={(item) => void setStatus('platforms', item)} /> : null}
    {state.data && tab === 'stores' ? <StoreList items={stores} platforms={platforms} edit={(item) => setEditor({ kind: 'store', item })} toggle={(item) => void setStatus('stores', item)} /> : null}
    {state.data && tab === 'sources' ? <SourceList items={sources} edit={(item) => setEditor({ kind: 'source', item })} toggle={(item) => void setStatus('sources', item)} /> : null}
    {tab === 'stores' && stores.length ? <p className="settings-footnote">更改所属平台会从当前生效日期生成店铺新版本，不会改写历史月份。</p> : null}
    {tab === 'sources' && sources.length ? <p className="settings-footnote">月度快照用于平台整月导出，修订文件会替换同月旧版本；增量用于持续追加，并按稳定业务键跨文件防重。</p> : null}
  </>
}

function PlatformList({ items, edit, toggle }: { items: PlatformResource[]; edit: (item: PlatformResource) => void; toggle: (item: PlatformResource) => void }) {
  if (!items.length) return <EmptyState title="还没有平台账号" description="先新增企业在电商平台上的账号，再建立店铺。" />
  return <ResourceTable headers={['平台账号', '平台', '外部标识', '状态', '操作']} rows={items.map((item) => [<strong>{item.name}</strong>, platformLabel(item.platform), item.external_account_id || '—', <Status value={item.status} />, <Actions item={item} edit={() => edit(item)} toggle={() => toggle(item)} />])} />
}

function StoreList({ items, platforms, edit, toggle }: { items: StoreResource[]; platforms: PlatformResource[]; edit: (item: StoreResource) => void; toggle: (item: StoreResource) => void }) {
  if (!items.length) return <EmptyState title="还没有店铺" description="新增店铺时需要选择所属平台账号和启用日期。" />
  const names = new Map(platforms.map((item) => [item.id, item.name]))
  return <ResourceTable headers={['店铺', '所属平台账号', '外部店铺标识', '启用日期', '状态', '操作']} rows={items.map((item) => [<strong>{item.name}</strong>, names.get(item.platform_account_id) || '历史平台版本', item.external_store_id || '—', item.activation_at?.slice(0, 10) || '—', <Status value={item.status} />, <Actions item={item} edit={() => edit(item)} toggle={() => toggle(item)} />])} />
}

function SourceList({ items, edit, toggle }: { items: SourceResource[]; edit: (item: SourceResource) => void; toggle: (item: SourceResource) => void }) {
  if (!items.length) return <EmptyState title="还没有数据内容" description="新增订单或费用文件规则后，首页会按月提示是否齐全。" />
  return <ResourceTable headers={['数据内容', '业务类型', '本月修订方式', '到达频率', '是否必需', '状态', '操作']} rows={items.map((item) => [<strong>{item.name}</strong>, sourceKind(item.source_kind), item.import_mode === 'incremental' ? '持续追加并防重' : '修订文件替换旧版', frequency(item.arrival_frequency), item.required === false ? '可选' : '必需', <Status value={item.status} />, <Actions item={item} edit={() => edit(item)} toggle={() => toggle(item)} />])} />
}

function Editor({ editor, platforms, sources, submit, close, busy, error }: { editor: EditorState; platforms: PlatformResource[]; sources: SourceResource[]; submit: (event: FormEvent<HTMLFormElement>) => void; close: () => void; busy: boolean; error: string }) {
  const item = editor.item
  const source = editor.kind === 'source' ? item as SourceResource | undefined : undefined
  const store = editor.kind === 'store' ? item as StoreResource | undefined : undefined
  const platform = editor.kind === 'platform' ? item as PlatformResource | undefined : undefined
  const title = `${item ? '编辑' : '新增'}${editor.kind === 'platform' ? '平台账号' : editor.kind === 'store' ? '店铺' : '数据内容'}`
  return <section className="settings-editor-panel" aria-labelledby="settings-editor-title"><div className="editor-heading"><div><p className="eyebrow">{item ? '从现在起生效' : '持续经营设置'}</p><h2 id="settings-editor-title">{title}</h2></div><button className="text-button" type="button" onClick={close}>取消</button></div><form onSubmit={submit}>
    <label>名称<input name="name" required autoFocus defaultValue={item?.name} /></label>
    {editor.kind === 'platform' ? <div className="form-grid"><label>电商平台<select name="platform" required defaultValue={platform?.platform || 'taobao'}><option value="taobao">淘宝 / 天猫</option><option value="jd">京东</option><option value="douyin">抖音电商</option><option value="shopify">Shopify</option><option value="generic">其他平台</option></select></label><label>平台账号标识<input name="external_account_id" defaultValue={platform?.external_account_id} placeholder="选填，例如 seller-10001" /></label></div> : null}
    {editor.kind === 'store' ? <div className="form-grid"><label>所属平台账号<select name="platform_account_id" required defaultValue={store?.platform_account_id || ''}><option value="" disabled>请选择平台账号</option>{platforms.filter((entry) => entry.status !== 'archived').map((entry) => <option key={entry.id} value={entry.id}>{entry.name}</option>)}</select></label><label>平台店铺标识<input name="external_store_id" defaultValue={store?.external_store_id} placeholder="选填，例如 shop-10001" /></label><label>启用日期<input name="activation_at" type="date" required defaultValue={store?.activation_at?.slice(0, 10)} /></label></div> : null}
    {editor.kind === 'source' ? <SourceFields source={source} sources={sources.filter((entry) => entry.id !== source?.id)} /> : null}
    {error ? <p className="form-error" role="alert">{error}</p> : null}<div className="editor-actions"><button className="button secondary" type="button" onClick={close}>取消</button><button className="button primary" disabled={busy || (editor.kind === 'store' && !platforms.length)}>{busy ? '正在保存…' : '保存并生效'}</button></div>
  </form></section>
}

function SourceFields({ source, sources }: { source?: SourceResource; sources: SourceResource[] }) {
  const aliases = (key: string, fallback: string) => source?.field_aliases?.[key]?.join('、') || fallback
  const requiredFields = new Set((source?.validations || []).filter((item) => item.type === 'required_field').map((item) => String(item.field)))
  const dependency = (source?.validations || []).find((item) => item.type === 'cross_source_match')?.dependency_source_logical_id
  const negative = new Set(Object.entries(source?.amount_directions || {}).filter(([, direction]) => direction === 'negative').map(([key]) => key))
  return <>
    <div className="form-grid"><label>启用日期<input name="activation_at" type="date" required defaultValue={source?.activation_at?.slice(0, 10)} /></label><label>这是什么数据？<select name="source_kind" defaultValue={source?.source_kind || 'orders'}><option value="orders">订单与销售</option><option value="fees">平台、广告或运费</option><option value="mixed">订单与费用混合</option></select></label><label>本月修订时如何处理？<select name="import_mode" defaultValue={source?.import_mode || 'monthly_snapshot'}><option value="monthly_snapshot">整月文件，以新版替换旧版</option><option value="incremental">持续追加，重复业务自动跳过</option></select></label><label>每行表示什么？<select name="data_granularity" defaultValue={source?.data_granularity || 'event'}><option value="event">一笔业务记录</option><option value="day">一天汇总</option><option value="month">一个月汇总</option></select></label><label>通常多久收到一次？<select name="arrival_frequency" defaultValue={source?.arrival_frequency || 'monthly'}><option value="monthly">每月</option><option value="daily">每天</option><option value="hourly">每小时</option><option value="adhoc">不定期</option></select></label><label>用什么避免重复？<select name="dedupe_strategy" defaultValue={source?.dedupe_keys?.includes('order_id') ? 'order' : 'event-date-store'}><option value="order">订单号</option><option value="event-date-store">业务类型 + 日期 + 店铺</option></select></label></div>
    <fieldset><legend>接受的文件</legend>{['xlsx', 'csv', 'zip'].map((kind) => <label className="inline-check" key={kind}><input type="checkbox" name="file_types" value={kind} defaultChecked={(source?.file_types || ['xlsx', 'csv']).includes(kind)} />{kind.toUpperCase()}</label>)}</fieldset>
    <details className="advanced-fields"><summary>文件列名和检查规则</summary><p>填写文件中可能出现的列名，多个名称用逗号隔开。系统只按这些确定规则识别。</p><div className="form-grid"><Alias name="date_aliases" label="业务日期列" value={aliases('occurred_at', '业务日期,date,transaction_date')} /><Alias name="order_aliases" label="订单号列" value={aliases('order_id', '订单号,order_id,order')} /><Alias name="store_aliases" label="店铺列" value={aliases('store_id', '店铺,store_id')} /><Alias name="sales_aliases" label="销售金额列" value={aliases('revenue', '销售额,revenue,sales')} /><Alias name="refund_aliases" label="退款金额列" value={aliases('refund', '退款额,refund')} /><Alias name="platform_fee_aliases" label="平台费用列" value={aliases('platform_fee', '平台费,platform_fee')} /><Alias name="advertising_fee_aliases" label="广告费用列" value={aliases('advertising_fee', '广告费,advertising_fee')} /><Alias name="shipping_fee_aliases" label="运费列" value={aliases('shipping_fee', '运费,shipping_fee')} /><Alias name="cost_aliases" label="商品成本列" value={aliases('product_cost', '商品成本,cost,product_cost')} /></div><fieldset><legend>必须出现的内容</legend>{[['occurred_at', '业务日期'], ['order_id', '订单号'], ['store_id', '店铺']].map(([value, label]) => <label className="inline-check" key={value}><input type="checkbox" name="required_fields" value={value} defaultChecked={requiredFields.has(value) || value === 'occurred_at'} />{label}</label>)}</fieldset><fieldset><legend>文件中以下金额为负数，需要转为扣减金额</legend>{[['refund', '退款'], ['platform_fee', '平台费'], ['advertising_fee', '广告费'], ['shipping_fee', '运费']].map(([value, label]) => <label className="inline-check" key={value}><input type="checkbox" name="negative_amount_fields" value={value} defaultChecked={negative.has(value)} />{label}</label>)}</fieldset><label>必须同时收到的本月文件<select name="dependency_source_logical_id" defaultValue={String(dependency || '')}><option value="">不要求其他文件</option>{sources.map((entry) => <option key={entry.id} value={entry.logical_id}>{entry.name}</option>)}</select></label></details>
    <label className="inline-check required-switch"><input type="checkbox" name="required" defaultChecked={source?.required !== false} />首页按月检查这项数据是否齐全</label>
  </>
}

function Alias({ name, label, value }: { name: string; label: string; value: string }) { return <label>{label}<input name={name} defaultValue={value} /></label> }
function Actions({ item, edit, toggle }: { item: Editable; edit: () => void; toggle: () => void }) { return <div className="row-actions"><button className="text-button" onClick={edit}><Pencil size={15} />编辑</button><button className="text-button" onClick={toggle}>{item.status === 'active' ? '停用' : '启用'}</button></div> }
function ResourceTable({ headers, rows }: { headers: string[]; rows: ReactNode[][] }) { return <section className="panel settings-table"><div className="table-scroll"><table><thead><tr>{headers.map((header) => <th key={header}>{header}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={index}>{row.map((cell, cellIndex) => <td key={cellIndex}>{cell}</td>)}</tr>)}</tbody></table></div></section> }
function TabButton({ active, onClick, icon, label, count }: { active: boolean; onClick: () => void; icon: ReactNode; label: string; count: number }) { return <button type="button" role="tab" aria-selected={active} className={active ? 'active' : ''} onClick={onClick}>{icon}<span>{label}</span><small>{count}</small></button> }
function Status({ value }: { value?: string }) { return <span className={`status-pill ${value === 'active' ? 'success' : 'neutral'}`}>{value === 'active' ? '使用中' : '已停用'}</span> }
function required(data: FormData, key: string) { return String(data.get(key) || '').trim() }
function optional(data: FormData, key: string) { return required(data, key) || undefined }
function csv(data: FormData, key: string) { return required(data, key).split(/[,，、]/).map((item) => item.trim()).filter(Boolean) }
function sourceKind(kind?: string) { return kind === 'fees' ? '费用' : kind === 'mixed' ? '订单与费用' : '订单与销售' }
function frequency(value?: string) { return value === 'daily' ? '每天' : value === 'hourly' ? '每小时' : value === 'adhoc' ? '不定期' : '每月' }
function platformLabel(value?: string) { return ({ taobao: '淘宝 / 天猫', jd: '京东', douyin: '抖音电商', shopify: 'Shopify', generic: '其他平台' } as Record<string, string>)[value || ''] || value || '—' }
