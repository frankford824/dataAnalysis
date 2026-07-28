import { useEffect, useState } from 'react'
import { ApiError } from '../api/http'
import { authorizeCorrection, confirmIngestion, listIngestions, publishIngestion, uploadFile, type RecognitionProblem } from '../api/ingestions'
import { listSources, listStores } from '../api/resources'
import { ErrorState, LoadingState } from '../components/AsyncState'
import PageHeader from '../components/PageHeader'
import { useAuth } from '../context/AuthContext'
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

function lockedCorrectionFrom(error: ApiError) {
  const body = error.details as { detail?: { code?: string; message?: string } } | undefined
  return error.status === 409 && body?.detail?.code === 'locked_correction_required' ? body.detail : null
}

export default function UploadPage() {
  const { canCorrectLocked } = useAuth()
  const [step, setStep] = useState(1)
  const [stores, setStores] = useState<StoreResource[]>([])
  const [sources, setSources] = useState<SourceResource[]>([])
  const [storeId, setStoreId] = useState('')
  const [sourceId, setSourceId] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [run, setRun] = useState<IngestionRun | null>(null)
  const [waitingRunIds, setWaitingRunIds] = useState<string[]>([])
  const [problem, setProblem] = useState<RecognitionProblem | null>(null)
  const [duplicate, setDuplicate] = useState(false)
  const [note, setNote] = useState('')
  const [correctionRequired, setCorrectionRequired] = useState(false)
  const [correctionReason, setCorrectionReason] = useState('')
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
      setCorrectionRequired(false)
      setCorrectionReason('')
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
      const locked = reason instanceof ApiError ? lockedCorrectionFrom(reason) : null
      if (locked) {
        setCorrectionRequired(true)
        setError(locked.message || '该月份已经锁定，需要企业管理员确认后才能更正。')
      } else {
        setError(reason instanceof ApiError && reason.status === 409 ? '仍有经营数据检查需要处理，全部通过后才能更新看板。' : reason instanceof Error ? reason.message : '经营数据尚未通过检查')
      }
    } finally {
      setBusy(false)
    }
  }

  const correctAndPublish = async () => {
    if (!run || correctionReason.trim().length < 10) return
    setBusy(true)
    setError('')
    try {
      await authorizeCorrection(run.id, correctionReason.trim())
      setRun(await publishIngestion(run.id))
      setCorrectionRequired(false)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '更正授权失败，请检查原因后重试')
    } finally {
      setBusy(false)
    }
  }

  const addAnother = () => {
    if (run) setWaitingRunIds((current) => current.includes(run.id) ? current : [...current, run.id])
    setRun(null)
    setFile(null)
    setSourceId('')
    setProblem(null)
    setDuplicate(false)
    setCorrectionRequired(false)
    setCorrectionReason('')
    setStep(2)
  }

  const continuePending = async () => {
    setBusy(true)
    setError('')
    try {
      const latest = await listIngestions()
      const next = latest.find((item) => waitingRunIds.includes(item.id) && ['awaiting_confirmation', 'quality_pending', 'quality_failed'].includes(item.status))
      if (!next) {
        setWaitingRunIds([])
        setError('没有仍需核对的文件；可直接查看经营看板。')
        return
      }
      setWaitingRunIds((current) => current.filter((id) => id !== next.id))
      setRun(next)
      setDuplicate(false)
      setNote('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法载入待核对文件')
    } finally {
      setBusy(false)
    }
  }

  return <>
    <PageHeader title="添加本月数据" description="三步完成范围选择、文件检查和经营看板更新。" />
    <UploadSteps current={step} />
    {loading ? <LoadingState label="正在加载可用范围…" /> : null}
    {error && !correctionRequired ? <ErrorState message={error} /> : null}
    {!loading && step === 1 ? <RangeStep stores={stores} sources={sources} storeId={storeId} sourceId={sourceId} onStore={setStoreId} onSource={setSourceId} onContinue={() => setStep(2)} /> : null}
    {step === 2 ? <FileStep file={file} busy={busy} problem={problem} selectedSource={sourceId} onFile={(next) => { if (next.size > MAX_FILE_BYTES) { setFile(null); setError('文件超过 100 MB，请拆分后重新选择。'); return } setError(''); setFile(next); setProblem(null) }} onSource={setSourceId} onUpload={() => void upload()} onBack={() => setStep(1)} /> : null}
    {step === 3 && run ? <ReviewStep run={run} storeName={stores.find((store) => store.id === run.store_id)?.name} duplicate={duplicate} busy={busy} note={note} onNote={setNote} onPublish={() => void publish()} onAddAnother={addAnother} onContinuePending={() => void continuePending()} hasPending={waitingRunIds.length > 0} correctionRequired={correctionRequired} canCorrect={canCorrectLocked} correctionReason={correctionReason} onCorrectionReason={setCorrectionReason} onCorrect={() => void correctAndPublish()} /> : null}
  </>
}
