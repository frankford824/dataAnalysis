import { createContext, useContext, useEffect, useMemo, useState, type Dispatch, type ReactNode, type SetStateAction } from 'react'
import { ApiClient } from '../api/client'
import { enterprises as demoEnterprises, platforms as demoPlatforms, stores as demoStores } from '../data/demo'
import type { ApiResource, FilterState } from '../types'

type Option = { id: string; name: string }
type AppState = {
  filters: FilterState
  setFilters: Dispatch<SetStateAction<FilterState>>
  api: ApiClient
  online: boolean
  setOnline: (v: boolean) => void
  enterpriseOptions: Option[]
  platformOptions: Option[]
  storeOptions: Option[]
}
const Context = createContext<AppState | null>(null)

export function AppProvider({ children }: { children: ReactNode }) {
  const [filters, setFilters] = useState<FilterState>({ enterpriseId: 'demo-enterprise', platformId: '', storeId: '', period: '2026-06' })
  const [online, setOnline] = useState(true)
  const [enterpriseOptions, setEnterpriseOptions] = useState<Option[]>(demoEnterprises)
  const [platformOptions, setPlatformOptions] = useState<Option[]>(demoPlatforms)
  const [storeOptions, setStoreOptions] = useState<Option[]>(demoStores)
  const platformApi = useMemo(() => new ApiClient(() => ({ userId: 'web-bootstrap', role: 'platform_admin' })), [])
  const api = useMemo(() => new ApiClient(() => ({ enterpriseId: filters.enterpriseId, userId: 'web-admin', role: 'admin' })), [filters.enterpriseId])
  useEffect(() => {
    let active = true
    platformApi.list<ApiResource>('enterprises').then(items => {
      if (!active || items.length === 0) return
      const options = items.map(({ id, name }) => ({ id, name }))
      setEnterpriseOptions(options)
      setFilters(current => options.some(option => option.id === current.enterpriseId)
        ? current
        : { ...current, enterpriseId: options[0].id, platformId: '', storeId: '' })
    }).catch(() => undefined)
    return () => { active = false }
  }, [platformApi])
  useEffect(() => {
    if (filters.enterpriseId.startsWith('demo-')) return
    let active = true
    Promise.all([api.list<ApiResource>('platforms'), api.list<ApiResource>('stores')]).then(([platformItems, storeItems]) => {
      if (!active) return
      setPlatformOptions([{ id: '', name: '全部平台' }, ...platformItems.map(({ id, name }) => ({ id, name }))])
      setStoreOptions([{ id: '', name: '全部店铺' }, ...storeItems.map(({ id, name }) => ({ id, name }))])
    }).catch(() => undefined)
    return () => { active = false }
  }, [api, filters.enterpriseId])
  return <Context.Provider value={{ filters, setFilters, api, online, setOnline, enterpriseOptions, platformOptions, storeOptions }}>{children}</Context.Provider>
}
export function useApp() { const value = useContext(Context); if (!value) throw new Error('AppProvider missing'); return value }
