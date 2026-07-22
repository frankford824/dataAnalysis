import { FormEvent, useState } from 'react'
import { createResource, listSources, listStores } from '../../api/resources'
import { ErrorState, LoadingState } from '../../components/AsyncState'
import PageHeader from '../../components/PageHeader'
import { useRequest } from '../../hooks/useRequest'
import type { SourceResource, StoreResource } from '../../types'

export default function DataSettingsPage() {
  const state = useRequest(() => Promise.all([listStores(), listSources()]), [])
  const [form, setForm] = useState<'store' | 'source' | null>(null)
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const values = Object.fromEntries(new FormData(event.currentTarget))
    setBusy(true)
    setMessage('')
    try {
      if (form === 'store') {
        await createResource<StoreResource>('stores', { name: values.name, activation_at: `${values.activation_at}T00:00:00Z` })
      } else {
        await createResource<SourceResource>('sources', {
          name: values.name,
          activation_at: `${values.activation_at}T00:00:00Z`,
          coverage_time_field: values.coverage_time_field,
          data_granularity: values.data_granularity,
          arrival_frequency: values.arrival_frequency,
          file_types: ['xlsx', 'csv', 'zip'],
          dedupe_keys: [],
          validations: [],
          required: true,
        })
      }
      setForm(null)
      setMessage('设置已保存')
      state.reload()
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : '保存失败')
    } finally {
      setBusy(false)
    }
  }

  return <>
    <PageHeader title="数据设置" description="管理店铺和需要收集的经营数据。生效日期之前的数据不会进入正式结果。" action={<div className="heading-actions"><button className="button secondary" onClick={() => setForm('store')}>添加店铺</button><button className="button primary" onClick={() => setForm('source')}>添加数据内容</button></div>} />
    {message ? <p className="notice" role="status">{message}</p> : null}
    {state.loading ? <LoadingState /> : null}
    {state.error ? <ErrorState message={state.error} retry={state.reload} /> : null}
    {state.data ? <div className="settings-columns"><ResourceList title="店铺" empty="还没有店铺" values={state.data[0]} /><ResourceList title="数据内容" empty="还没有数据内容" values={state.data[1]} /></div> : null}
    {form ? <div className="modal-backdrop" onMouseDown={() => setForm(null)}><section className="modal" role="dialog" aria-modal="true" onMouseDown={(event) => event.stopPropagation()}><h2>{form === 'store' ? '添加店铺' : '添加数据内容'}</h2><form onSubmit={submit}><label>名称<input name="name" required /></label><label>生效日期<input name="activation_at" type="date" required /></label>{form === 'source' ? <><label>文件中的业务日期字段<input name="coverage_time_field" required /></label><label>数据明细程度<select name="data_granularity"><option value="event">每笔记录</option><option value="day">每日</option><option value="month">每月</option></select></label><label>通常多久收到一次<select name="arrival_frequency"><option value="daily">每日</option><option value="monthly">每月</option><option value="adhoc">不定期</option></select></label></> : null}<div className="modal-actions"><button type="button" className="button secondary" onClick={() => setForm(null)}>取消</button><button className="button primary" disabled={busy}>{busy ? '正在保存…' : '保存'}</button></div></form></section></div> : null}
  </>
}

function ResourceList({ title, empty, values }: { title: string; empty: string; values: Array<{ id: string; name: string; status?: string }> }) {
  return <section className="panel resource-list"><h2>{title}</h2>{values.length === 0 ? <p>{empty}</p> : <ul>{values.map((value) => <li key={value.id}><strong>{value.name}</strong><span>{value.status === 'active' ? '使用中' : value.status || '草稿'}</span></li>)}</ul>}</section>
}
