import { useEffect, useState } from 'react'
import { ApiError } from '../api/http'
import { confirmIngestion, publishIngestion, uploadFile, type RecognitionProblem } from '../api/ingestions'
import { listSources, listStores } from '../api/resources'
import { ErrorState, LoadingState } from '../components/AsyncState'
import PageHeader from '../components/PageHeader'
import type { IngestionRun, SourceResource, StoreResource } from '../types'
import FileStep from './upload/FileStep'
import RangeStep from './upload/RangeStep'
import ReviewStep from './upload/ReviewStep'
import UploadSteps from './upload/UploadSteps'

const MAX_FILE_BYTES = 100 * 1024 * 1024

function recognitionFrom(error: ApiError) {
  const body = error.details as { detail?: RecognitionProblem } | undefined
  return error.status === 409 && body?.detail?.options ? body.detail : null
}

export default function UploadPage() {
  const [step, setStep] = useState(1)
  const [stores, setStores] = useState<StoreResource[]>([])
  const [sources, setSources] = useState<SourceResource[]>([])
  const [storeId, setStoreId] = useState('')
  const [sourceId, setSourceId] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [run, setRun] = useState<IngestionRun | null>(null)
  const [problem, setProblem] = useState<RecognitionProblem | null>(null)
  const [duplicate, setDuplicate] = useState(false)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    Promise.all([listStores(), listSources()]).then(([nextStores, nextSources]) => {
      setStores(nextStores)
      setSources(nextSources)
      if (nextStores.length === 1) setStoreId(nextStores[0].id)
      if (nextSources.length === 1) setSourceId(nextSources[0].id)
    }).catch((reason) => setError(reason instanceof Error ? reason.message : '无法加载经营范围')).finally(() => setLoading(false))
  }, [])

  const upload = async () => {
    if (!file) return
    if (file.size > MAX_FILE_BYTES) {
      setError('文件超过 100 MB，请拆分后重新选择。')
      return
    }
    setBusy(true)
    setError('')
    try {
      const next = await uploadFile(file, sourceId || undefined, storeId || undefined)
      setRun(next)
      setDuplicate(Boolean((next as IngestionRun & { deduplicated?: boolean }).deduplicated))
      setProblem(null)
      setStep(3)
    } catch (reason) {
      const recognition = reason instanceof ApiError ? recognitionFrom(reason) : null
      if (recognition) setProblem(recognition)
      else setError(reason instanceof Error ? reason.message : '上传失败')
    } finally {
      setBusy(false)
    }
  }

  const publish = async () => {
    if (!run) return
    setBusy(true)
    setError('')
    try {
      await confirmIngestion(run.id, true, note || undefined)
      setRun(await publishIngestion(run.id))
    } catch (reason) {
      setError(reason instanceof ApiError && reason.status === 409 ? '仍有经营数据检查需要处理，全部通过后才能更新看板。' : reason instanceof Error ? reason.message : '经营数据尚未通过检查')
    } finally {
      setBusy(false)
    }
  }

  return <>
    <PageHeader title="添加本月数据" description="三步完成范围选择、文件检查和经营看板更新。" />
    <UploadSteps current={step} />
    {loading ? <LoadingState label="正在加载可用范围…" /> : null}
    {error ? <ErrorState message={error} /> : null}
    {!loading && step === 1 ? <RangeStep stores={stores} sources={sources} storeId={storeId} sourceId={sourceId} onStore={setStoreId} onSource={setSourceId} onContinue={() => setStep(2)} /> : null}
    {step === 2 ? <FileStep file={file} busy={busy} problem={problem} selectedSource={sourceId} onFile={(next) => { if (next.size > MAX_FILE_BYTES) { setFile(null); setError('文件超过 100 MB，请拆分后重新选择。'); return } setError(''); setFile(next); setProblem(null) }} onSource={setSourceId} onUpload={() => void upload()} onBack={() => setStep(1)} /> : null}
    {step === 3 && run ? <ReviewStep run={run} storeName={stores.find((store) => store.id === run.store_id)?.name} duplicate={duplicate} busy={busy} note={note} onNote={setNote} onPublish={() => void publish()} /> : null}
  </>
}
