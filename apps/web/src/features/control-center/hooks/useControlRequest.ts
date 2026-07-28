import { useCallback, useEffect, useState } from 'react'

export type ControlRequestState<T> = {
  data: T | null
  loading: boolean
  error: string | null
  reload: () => void
  setData: (next: T | null | ((current: T | null) => T | null)) => void
}

export function useControlRequest<T>(loader: () => Promise<T>, dependencies: readonly unknown[] = []): ControlRequestState<T> {
  const [version, setVersion] = useState(0)
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const reload = useCallback(() => setVersion((current) => current + 1), [])

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setError(null)
    loader().then(
      (next) => {
        if (controller.signal.aborted) return
        setData(next)
        setLoading(false)
      },
      (reason: unknown) => {
        if (controller.signal.aborted) return
        setError(reason instanceof Error ? reason.message : '暂时无法加载')
        setLoading(false)
      },
    )
    return () => controller.abort()
    // Callers pass stable loaders or explicit primitive dependencies.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...dependencies, version])

  return { data, loading, error, reload, setData }
}
