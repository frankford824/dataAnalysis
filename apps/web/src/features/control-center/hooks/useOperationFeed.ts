import { useCallback, useEffect, useRef, useState } from 'react'
import { listOperations, operationStreamUrl } from '../api'
import type { OperationEvent, OperationsResponse, ProgressConnectionState } from '../types'

type EventSourceLike = Pick<EventSource, 'close' | 'onerror' | 'onmessage' | 'onopen'>
type EventSourceFactory = (url: string) => EventSourceLike

const DEFAULT_POLL_INTERVAL = 10_000
const DEFAULT_RECONNECT_INTERVAL = 30_000
const defaultEventSourceFactory: EventSourceFactory = (url) => new EventSource(url, { withCredentials: true })

function mergeEvent(response: OperationsResponse | null, event: OperationEvent): OperationsResponse | null {
  if (!response) return response
  return {
    ...response,
    updated_at: event.occurred_at,
    items: response.items.map((operation) => operation.id !== event.operation_id ? operation : {
      ...operation,
      status: event.status,
      stage: event.stage,
      updated_at: event.occurred_at,
      timeline: operation.timeline.some((entry) => entry.id === event.id)
        ? operation.timeline
        : [...operation.timeline, event],
    }),
  }
}

export function useOperationFeed(options: {
  eventSourceFactory?: EventSourceFactory
  pollIntervalMs?: number
  reconnectIntervalMs?: number
} = {}) {
  const {
    eventSourceFactory = defaultEventSourceFactory,
    pollIntervalMs = DEFAULT_POLL_INTERVAL,
    reconnectIntervalMs = DEFAULT_RECONNECT_INTERVAL,
  } = options
  const [data, setData] = useState<OperationsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [connection, setConnection] = useState<ProgressConnectionState>('connecting')
  const [lastUpdatedAt, setLastUpdatedAt] = useState<string | null>(null)
  const sourceRef = useRef<EventSourceLike | null>(null)
  const pollRef = useRef<number | null>(null)
  const reconnectRef = useRef<number | null>(null)
  const activeRef = useRef(true)

  const refresh = useCallback(async () => {
    try {
      const response = await listOperations()
      if (!activeRef.current) return
      setData(response)
      setLastUpdatedAt(response.updated_at)
      setError(null)
      setLoading(false)
    } catch (reason) {
      if (!activeRef.current) return
      setError(reason instanceof Error ? reason.message : '无法取得处理进度')
      setConnection('offline')
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    activeRef.current = true

    const stopPolling = () => {
      if (pollRef.current !== null) window.clearInterval(pollRef.current)
      pollRef.current = null
    }

    const startPolling = () => {
      if (pollRef.current !== null) return
      setConnection('polling')
      void refresh()
      pollRef.current = window.setInterval(() => void refresh(), pollIntervalMs)
    }

    const connect = () => {
      sourceRef.current?.close()
      setConnection((current) => current === 'polling' ? current : 'connecting')
      const source = eventSourceFactory(operationStreamUrl())
      sourceRef.current = source
      source.onopen = () => {
        if (!activeRef.current) return
        stopPolling()
        setConnection('live')
        setError(null)
      }
      source.onmessage = (message) => {
        if (!activeRef.current) return
        try {
          const event = JSON.parse(message.data) as OperationEvent
          setData((current) => mergeEvent(current, event))
          setLastUpdatedAt(event.occurred_at)
        } catch {
          setError('收到无法识别的进度消息，已保留最近一次有效状态')
        }
      }
      source.onerror = () => {
        if (!activeRef.current) return
        source.close()
        startPolling()
        if (reconnectRef.current !== null) window.clearTimeout(reconnectRef.current)
        reconnectRef.current = window.setTimeout(connect, reconnectIntervalMs)
      }
    }

    void refresh()
    connect()
    return () => {
      activeRef.current = false
      sourceRef.current?.close()
      stopPolling()
      if (reconnectRef.current !== null) window.clearTimeout(reconnectRef.current)
    }
  }, [eventSourceFactory, pollIntervalMs, reconnectIntervalMs, refresh])

  return { data, loading, error, connection, lastUpdatedAt, reload: refresh }
}
