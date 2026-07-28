import type { PlatformResource, Resource, SourceResource, StoreResource } from '../types'
import { request } from './http'

export const listStores = () => request<StoreResource[]>('/stores')
export const listSources = () => request<SourceResource[]>('/sources')
export const listPlatforms = () => request<PlatformResource[]>('/platforms')
export const listDashboards = () => request<Resource[]>('/dashboards')

export function createResource<T extends Resource>(name: string, body: Record<string, unknown>) {
  return request<T>(`/${name}`, { method: 'POST', body: JSON.stringify(body) })
}

export function updateResource<T extends Resource>(name: string, id: string, body: Record<string, unknown>) {
  return request<T>(`/${name}/${id}`, { method: 'PATCH', body: JSON.stringify(body) })
}
