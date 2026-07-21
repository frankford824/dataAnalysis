export type ApiResource = { id: string; name: string; status?: string; [key: string]: unknown }
export type FilterState = { enterpriseId: string; platformId: string; storeId: string; period: string }
export type UploadItem = { id: string; serverId?: string; file: File; state: 'uploading' | 'ready' | 'confirm' | 'failed'; message?: string; progress: number }

export type DashboardSummary = {
  revenue: number; refund: number; fees: number; profit: number
  trend: { month: string; revenue: number; profit: number }[]
  stores: { id: string; name: string; revenue: number; change: number; refund: number; refundRate: number; fees: number; profit: number; profitChange: number }[]
}
