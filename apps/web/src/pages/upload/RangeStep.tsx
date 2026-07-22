import type { SourceResource, StoreResource } from '../../types'

export default function RangeStep({
  stores,
  sources,
  storeId,
  sourceId,
  onStore,
  onSource,
  onContinue,
}: {
  stores: StoreResource[]
  sources: SourceResource[]
  storeId: string
  sourceId: string
  onStore: (id: string) => void
  onSource: (id: string) => void
  onContinue: () => void
}) {
  return <section className="flow-panel">
    <h2>这份数据属于哪里？</h2>
    <p>选择店铺可以提高识别准确度；跨店铺文件可选择“由文件自动识别”。</p>
    <div className="form-grid">
      <label>店铺范围<select value={storeId} onChange={(event) => onStore(event.target.value)}><option value="">由文件自动识别或包含多个店铺</option>{stores.map((store) => <option key={store.id} value={store.id}>{store.name}</option>)}</select></label>
      <label>数据内容<select value={sourceId} onChange={(event) => onSource(event.target.value)}><option value="">自动识别</option>{sources.map((source) => <option key={source.id} value={source.id}>{source.name}</option>)}</select></label>
    </div>
    <button className="button primary flow-action" onClick={onContinue}>继续添加文件</button>
  </section>
}
