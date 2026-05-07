import { useCallback, useEffect, useRef, useState } from 'react'

export type SSEStatus = 'connecting' | 'open' | 'error'

export interface UseSSEResult<T> {
  value: T | null
  status: SSEStatus
  failureCount: number
  reconnect: () => void
}

interface UseSSEOptions<T> {
  topic: string
  parser: (event: MessageEvent<string>) => T | null
  /**
   * Path to the SSE endpoint. Defaults to `/api/events`. Override only in tests.
   */
  endpoint?: string
  /**
   * Inject an EventSource constructor. Tests pass a fake; production uses
   * the global `EventSource`.
   */
  factory?: (url: string) => EventSource
}

const INITIAL_BACKOFF_MS = 500
const MAX_BACKOFF_MS = 30_000

/**
 * Open a long-lived EventSource and surface the latest message of `topic`
 * to the component. Auto-reconnects with exponential backoff capped at 30s.
 *
 * Cleanup: on unmount, the EventSource is closed and any pending
 * reconnect timer is cleared. Calling `reconnect()` resets the backoff
 * counter and forces an immediate reopen.
 */
export function useSSE<T>({
  topic,
  parser,
  endpoint = '/api/events',
  factory,
}: UseSSEOptions<T>): UseSSEResult<T> {
  const [value, setValue] = useState<T | null>(null)
  const [status, setStatus] = useState<SSEStatus>('connecting')
  const [failureCount, setFailureCount] = useState(0)

  const sourceRef = useRef<EventSource | null>(null)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const failureCountRef = useRef(0)
  const manualReconnectRef = useRef(false)

  // Stash unstable callbacks in refs so `open`'s identity does not depend
  // on caller-supplied closures. Without this, an inline `factory={() => ...}`
  // in tests or callers causes `useCallback` to recreate `open` every render,
  // which thrashes the mount effect and reopens the EventSource in a loop.
  const parserRef = useRef(parser)
  const factoryRef = useRef(factory)
  // eslint-disable-next-line react-hooks/refs -- latest-ref pattern; safe because ref writes don't trigger re-render
  parserRef.current = parser
  // eslint-disable-next-line react-hooks/refs -- latest-ref pattern; safe because ref writes don't trigger re-render
  factoryRef.current = factory

  const open = useCallback(() => {
    if (sourceRef.current !== null) {
      sourceRef.current.close()
      sourceRef.current = null
    }
    setStatus('connecting')
    const make =
      factoryRef.current ?? ((url: string) => new EventSource(url, { withCredentials: true }))
    const src = make(endpoint)
    sourceRef.current = src

    src.addEventListener('open', () => {
      setStatus('open')
      failureCountRef.current = 0
      setFailureCount(0)
    })

    src.addEventListener(topic, (raw: Event) => {
      const ev = raw as MessageEvent<string>
      const parsed = parserRef.current(ev)
      if (parsed !== null) {
        setValue(parsed)
      }
    })

    src.addEventListener('error', () => {
      src.close()
      sourceRef.current = null
      if (manualReconnectRef.current) {
        manualReconnectRef.current = false
        setStatus('error')
        return
      }
      failureCountRef.current += 1
      setFailureCount(failureCountRef.current)
      setStatus('error')
    })
  }, [endpoint, topic])

  // Mount: open the connection. Cleans up on unmount or when the keyed
  // primitives (endpoint/topic) change. Stable across re-renders triggered
  // by state updates.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- intentional: open() initializes connection state on mount
    open()
    return () => {
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current)
        timerRef.current = null
      }
      if (sourceRef.current !== null) {
        sourceRef.current.close()
        sourceRef.current = null
      }
    }
  }, [open])

  // Schedule a backoff reconnect whenever we transition into 'error' with
  // a non-zero failure count. Decoupling this from the EventSource error
  // handler removes the need for an `openRef` forward-declaration trick
  // and lets fake timers drive the test deterministically.
  useEffect(() => {
    if (status !== 'error' || failureCount === 0) {
      return
    }
    const delay = Math.min(INITIAL_BACKOFF_MS * 2 ** (failureCount - 1), MAX_BACKOFF_MS)
    const timer = setTimeout(() => {
      timerRef.current = null
      open()
    }, delay)
    timerRef.current = timer
    return () => {
      clearTimeout(timer)
      if (timerRef.current === timer) {
        timerRef.current = null
      }
    }
  }, [status, failureCount, open])

  const reconnect = useCallback(() => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current)
      timerRef.current = null
    }
    failureCountRef.current = 0
    setFailureCount(0)
    manualReconnectRef.current = true
    open()
  }, [open])

  return { value, status, failureCount, reconnect }
}
