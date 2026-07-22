import { useEffect, useState } from 'react'
import { listPlatforms, listStores } from '../api/resources'
import { useFilters } from '../context/FilterContext'
import type { Resource, StoreResource } from '../types'

export default function BusinessFilters() {
  const { filters, setFilters } = useFilters()
  const [stores, setStores] = useState<StoreResource[]>([])
  const [platforms, setPlatforms] = useState<Resource[]>([])

  useEffect(() => {
    Promise.all([listStores(), listPlatforms()])
      .then(([storeRows, platformRows]) => { setStores(storeRows); setPlatforms(platformRows) })
      .catch(() => { setStores([]); setPlatforms([]) })
  }, [])

  const visibleStores = filters.platformId
    ? stores.filter((store) => store.platform_account_id === filters.platformId)
    : stores

  return <section className="business-filters" aria-label="经营范围">
    <label>平台<select value={filters.platformId} onChange={(event) => setFilters({ ...filters, platformId: event.target.value, storeIds: [] })}><option value="">全部平台</option>{platforms.map((platform) => <option value={platform.id} key={platform.id}>{platform.name}</option>)}</select></label>
    <label>店铺<select value={filters.storeIds[0] || ''} onChange={(event) => setFilters({ ...filters, storeIds: event.target.value ? [event.target.value] : [] })}><option value="">全部有权店铺</option>{visibleStores.map((store) => <option value={store.id} key={store.id}>{store.name}</option>)}</select></label>
    <label>开始日期<input type="date" value={filters.dateFrom} max={filters.dateTo} onChange={(event) => setFilters({ ...filters, dateFrom: event.target.value })} /></label>
    <label>结束日期<input type="date" value={filters.dateTo} min={filters.dateFrom} onChange={(event) => setFilters({ ...filters, dateTo: event.target.value })} /></label>
  </section>
}
