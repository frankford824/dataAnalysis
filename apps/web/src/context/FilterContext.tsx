import { createContext, useContext, useMemo, useState, type ReactNode } from 'react'
import type { FilterState } from '../types'

function initialFilters(): FilterState {
  const today = new Date()
  const first = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), 1))
  const last = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth() + 1, 0))
  return { platformId: '', storeIds: [], dateFrom: first.toISOString().slice(0, 10), dateTo: last.toISOString().slice(0, 10) }
}

type FilterValue = { filters: FilterState; setFilters: (filters: FilterState) => void }
const FilterContext = createContext<FilterValue | null>(null)

export function FilterProvider({ children }: { children: ReactNode }) {
  const [filters, setFilters] = useState<FilterState>(initialFilters)
  const value = useMemo(() => ({ filters, setFilters }), [filters])
  return <FilterContext.Provider value={value}>{children}</FilterContext.Provider>
}

export function useFilters() {
  const value = useContext(FilterContext)
  if (!value) throw new Error('FilterProvider missing')
  return value
}
