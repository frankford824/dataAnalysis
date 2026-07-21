import type { ApiResource, DashboardSummary, FilterState } from '../types'

const API_BASE = import.meta.env.VITE_API_BASE_URL || import.meta.env.VITE_API_BASE || '/api/v1'

export class ApiError extends Error { constructor(public status: number, message: string) { super(message) } }

export class ApiClient {
  constructor(private getContext: () => { enterpriseId?: string; userId?: string; role?: string }) {}
  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const ctx = this.getContext()
    const response = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: {
        ...(init.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
        ...(ctx.enterpriseId ? { 'X-Enterprise-ID': ctx.enterpriseId } : {}),
        'X-User-ID': ctx.userId || 'web-user', 'X-Role': ctx.role || 'admin', ...init.headers,
      },
    })
    if (!response.ok) { const body = await response.json().catch(() => null); throw new ApiError(response.status, body?.detail || `请求失败 (${response.status})`) }
    if (response.status === 204) return undefined as T
    return response.json()
  }
  list<T = ApiResource>(resource: string) { return this.request<T[]>(`/${resource}`) }
  create<T = ApiResource>(resource: string, body: unknown) { return this.request<T>(`/${resource}`, { method: 'POST', body: JSON.stringify(body) }) }
  update<T = ApiResource>(resource: string, id: string, body: unknown) { return this.request<T>(`/${resource}/${id}`, { method: 'PATCH', body: JSON.stringify(body) }) }
  remove(resource: string, id: string) { return this.request<{ id: string; action: string }>(`/${resource}/${id}`, { method: 'DELETE' }) }
  upload(file: File, sourceId: string, storeId?: string, backfill = false) {
    const data = new FormData(); data.append('file', file); data.append('source_definition_id', sourceId); if (storeId) data.append('store_id', storeId); data.append('backfill', String(backfill))
    return this.request<{ id: string; status: string; duplicate?: boolean }>('/ingestions/upload', { method: 'POST', body: data })
  }
  initiateResumable(body: { filename: string; size: number; sha256: string; source_definition_id: string; store_id?: string }) { return this.request<{ deduplicated: boolean; upload_id?: string; part_size?: number; received_parts?: number[]; ingestion?: { id: string; status: string } }>('/ingestions/upload/initiate', { method: 'POST', body: JSON.stringify(body) }) }
  uploadPart(uploadId: string, part: number, bytes: Uint8Array) { return this.request<{ part_number: number }>(`/ingestions/upload/${uploadId}/parts/${part}`, { method: 'PUT', body: bytes as unknown as BodyInit, headers: { 'Content-Type': 'application/octet-stream' } }) }
  completeResumable(uploadId: string) { return this.request<{ id: string; status: string; duplicate?: boolean }>(`/ingestions/upload/${uploadId}/complete`, { method: 'POST' }) }
  confirmIngestion(id: string, accepted: boolean, note?: string) { return this.request(`/ingestions/${id}/confirm`, { method: 'POST', body: JSON.stringify({ accepted, note }) }) }
  publishIngestion(id: string) { return this.request(`/ingestions/${id}/publish`, { method: 'POST' }) }
  dashboardEmbedToken(id: string) { return this.request<{ token: string; embedded_id: string; expires_in: number }>(`/dashboards/${id}/embed-token`, { method: 'POST' }) }
  ask(question: string, filters: FilterState) { const start=new Date(`${filters.period}-01T00:00:00Z`);const end=new Date(Date.UTC(start.getUTCFullYear(),start.getUTCMonth()+1,1)-1);return this.request<{ answer: string; options?: string[]; data?: Record<string, unknown>[] }>('/business-questions', { method: 'POST', body: JSON.stringify({ question, store_ids: filters.storeId ? [filters.storeId] : [], date_from: start.toISOString(), date_to: end.toISOString() }) }) }
  async dashboard(filters: FilterState): Promise<DashboardSummary> {
    const selected = new Date(`${filters.period}-01T00:00:00Z`), start = new Date(Date.UTC(selected.getUTCFullYear(), selected.getUTCMonth()-11, 1)), end = new Date(Date.UTC(selected.getUTCFullYear(), selected.getUTCMonth()+1, 1))
    const store = filters.storeId ? ` AND store_id = '${filters.storeId.replaceAll("'", "''")}'` : ''
    const sql = `SELECT period_start, store_id, SUM(revenue) AS revenue, SUM(refund) AS refund, SUM(fees) AS fees, SUM(profit) AS profit FROM certified_sales WHERE period_start >= '${start.toISOString()}' AND period_start < '${end.toISOString()}'${store} GROUP BY period_start, store_id ORDER BY period_start`
    const [result, stores] = await Promise.all([
      this.request<{ columns: string[]; rows: (string|number|null)[][] }>('/certified-query', { method:'POST', body:JSON.stringify({sql}) }),
      this.list<ApiResource>('stores'),
    ])
    const index=Object.fromEntries(result.columns.map((c,i)=>[c,i])); const values=(name:string,row:(string|number|null)[])=>Number(row[index[name]]||0); const selectedPrefix=filters.period
    const current=result.rows.filter(r=>String(r[index.period_start]).startsWith(selectedPrefix)); const names=Object.fromEntries(stores.map(store => [store.id, store.name]))
    const byMonth=new Map<string,{revenue:number;profit:number}>(); for(const row of result.rows){const month=String(row[index.period_start]).slice(0,7);const old=byMonth.get(month)||{revenue:0,profit:0};old.revenue+=values('revenue',row);old.profit+=values('profit',row);byMonth.set(month,old)}
    const byStore=new Map<string,{revenue:number;refund:number;fees:number;profit:number}>(); for(const row of current){const id=String(row[index.store_id]);const old=byStore.get(id)||{revenue:0,refund:0,fees:0,profit:0};old.revenue+=values('revenue',row);old.refund+=values('refund',row);old.fees+=values('fees',row);old.profit+=values('profit',row);byStore.set(id,old)}
    return { revenue:current.reduce((n,r)=>n+values('revenue',r),0), refund:current.reduce((n,r)=>n+values('refund',r),0), fees:current.reduce((n,r)=>n+values('fees',r),0), profit:current.reduce((n,r)=>n+values('profit',r),0), trend:Array.from(byMonth,([month,v])=>({month,...v})), stores:Array.from(byStore,([id,value])=>({id,name:names[id]||'未命名店铺',...value,change:0,refundRate:value.revenue?Number((value.refund/value.revenue*100).toFixed(2)):0,profitChange:0})) }
  }
  export(filters: FilterState) { const q = new URLSearchParams({ period: filters.period, ...(filters.storeId ? { store_id: filters.storeId } : {}) }); return fetch(`${API_BASE}/exports/certified?${q}`, { headers: { 'X-Enterprise-ID': filters.enterpriseId, 'X-User-ID': 'web-user', 'X-Role': 'analyst' } }) }
}
