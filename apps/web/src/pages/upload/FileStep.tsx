import { CloudUpload, FileSpreadsheet } from 'lucide-react'
import { useRef } from 'react'
import type { RecognitionOption } from '../../api/ingestions'

const accepted = '.xlsx,.xls,.csv,.zip'

export default function FileStep({
  file,
  busy,
  problem,
  selectedSource,
  onFile,
  onSource,
  onUpload,
  onBack,
}: {
  file: File | null
  busy: boolean
  problem: { message: string; options: RecognitionOption[] } | null
  selectedSource: string
  onFile: (file: File) => void
  onSource: (id: string) => void
  onUpload: () => void
  onBack: () => void
}) {
  const input = useRef<HTMLInputElement>(null)
  return <section className="flow-panel">
    <div className="dropzone" onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); const dropped = event.dataTransfer.files[0]; if (dropped) onFile(dropped) }}>
      <CloudUpload />
      <h2>把文件拖到这里</h2>
      <p>支持 Excel、CSV 和 ZIP，单个文件不超过 100 MB；同一文件不会重复入账。</p>
      <button className="button secondary" onClick={() => input.current?.click()}>选择文件</button>
      <input ref={input} aria-label="选择经营文件" className="visually-hidden-file" type="file" accept={accepted} onChange={(event) => { const selected = event.target.files?.[0]; if (selected) onFile(selected) }} />
    </div>
    {file ? <div className="selected-file"><FileSpreadsheet /><div><strong>{file.name}</strong><span>{(file.size / 1024).toFixed(0)} KB</span></div></div> : null}
    {problem ? <fieldset className="recognition-choice"><legend>{problem.message}</legend>{problem.options.map((option) => <label key={option.id}><input type="radio" name="source" value={option.id} checked={selectedSource === option.id} onChange={() => onSource(option.id)} />{option.label}</label>)}</fieldset> : null}
    <div className="flow-buttons"><button className="button secondary" onClick={onBack}>返回</button><button className="button primary" disabled={!file || busy || Boolean(problem && !selectedSource)} onClick={onUpload}>{busy ? '正在安全上传…' : problem ? '按此内容重新识别' : '上传并检查'}</button></div>
  </section>
}
