import { useCallback, useEffect, useState } from 'react'

export type RequestState<T> = { data: T | null; loading: boolean; error: string | null }

export function useRequest<T>(loader: () => Promise<T>, dependencies: readonly unknown[]) {
  const [version, setVersion] = useState(0)
  const [state, setState] = useState<RequestState<T>>({ data: null, loading: true, error: null })
  const reload = useCallback(() => setVersion((value) => value + 1), [])

  useEffect(() => {
    let active = true
    setState((previous) => ({ ...previous, loading: true, error: null }))
    loader().then(
      (data) => { if (active) setState({ data, loading: false, error: null }) },
      (error: unknown) => { if (active) setState({ data: null, loading: false, error: error instanceof Error ? error.message : '加载失败' }) },
    )
    return () => { active = false }
    // The caller owns a stable loader or explicit primitive dependencies.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...dependencies, version])

  return { ...state, reload }
}
