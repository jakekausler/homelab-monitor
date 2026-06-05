import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'

import type { LogLine } from '@/components/logs/types'

/** Minimal EventSource surface the hook uses. Mirrors sse.ts's approach of
 *  satisfying strict typing while allowing a FakeEventSource in tests. */
export interface EventSourceLike {
  addEventListener(type: string, listener: (event: Event) => void): void
  close(): void
  onopen: ((this: EventSourceLike, ev: Event) => unknown) | null
  onerror: ((this: EventSourceLike, ev: Event) => unknown) | null
}

export type LogsTailStatus = 'idle' | 'connecting' | 'open' | 'error'

export interface LogsTailError {
  code: string
  message: string
  retryAfter?: number
}

export interface UseLogsTailResult {
  status: LogsTailStatus
  error: LogsTailError | null
  reconnect(): void
}

export interface UseLogsTailOptions {
  enabled: boolean
  onLines?: (batch: LogLine[]) => void
  /** Test injection — mirror sse.ts's factory param. */
  factory?: (url: string) => EventSourceLike
  /** Test injection for failure classification. */
  fetchImpl?: typeof fetch
  /** Buffer cap; default 1000. */
  bufferCap?: number
}

const INITIAL_BACKOFF_MS = 500
const MAX_BACKOFF_MS = 30_000
const DEFAULT_BATCH_CAP = 1000

function buildTailUrl(expr: string, services: string): string {
  const params = new URLSearchParams()
  params.set('expr', expr)
  if (services.length > 0) params.set('services', services)
  return `/api/logs/tail?${params.toString()}`
}

export function useLogsTail(
  expr: string,
  services: string,
  opts: UseLogsTailOptions,
): UseLogsTailResult {
  // State (drives re-render)
  const [status, setStatus] = useState<LogsTailStatus>('idle')
  const [error, setError] = useState<LogsTailError | null>(null)
  const [failureCount, setFailureCount] = useState(0)

  // Refs (no re-render)
  const sourceRef = useRef<EventSourceLike | null>(null)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const bufferRef = useRef<LogLine[]>([])
  const droppedRef = useRef(0)
  const failureCountRef = useRef(0)
  const rafRef = useRef<number | null>(null)
  const openedRef = useRef(false)
  const classifyDoneRef = useRef(false)
  const abortRef = useRef<AbortController | null>(null)

  // Latest-ref pattern for unstable callbacks — kept in a layout effect so
  // we never write .current during render (react-x rule).
  const factoryRef = useRef(opts.factory)
  const fetchImplRef = useRef(opts.fetchImpl)
  const onLinesRef = useRef(opts.onLines)
  // no deps: refresh the latest-ref values on every render
  useLayoutEffect(() => {
    factoryRef.current = opts.factory
    fetchImplRef.current = opts.fetchImpl
    onLinesRef.current = opts.onLines
  })

  const cap = opts.bufferCap ?? DEFAULT_BATCH_CAP

  // Flush scheduling (rAF)
  const scheduleFlush = useCallback(() => {
    if (rafRef.current !== null) return
    // Mark a flush as pending BEFORE scheduling so a synchronous rAF callback
    // (e.g. a test stub that invokes the callback immediately, or a re-entrant
    // frame) cannot leave a stale id behind: the callback clears the flag itself,
    // and we only overwrite rafRef with the real id if it's still pending.
    rafRef.current = -1
    const flush = (): void => {
      rafRef.current = null
      const batch = bufferRef.current
      bufferRef.current = []
      if (batch.length > 0) onLinesRef.current?.(batch)
    }
    const id = requestAnimationFrame(flush)
    // Only store the real id if the flush hasn't already run synchronously.
    if (rafRef.current === -1) rafRef.current = id
  }, [])

  const cancelFlush = useCallback(() => {
    if (rafRef.current !== null) {
      cancelAnimationFrame(rafRef.current)
      rafRef.current = null
    }
  }, [])

  // The open() callback
  const open = useCallback(() => {
    // Close any existing source
    if (sourceRef.current !== null) {
      sourceRef.current.close()
      sourceRef.current = null
    }

    // Reset per-open-cycle flags
    openedRef.current = false
    classifyDoneRef.current = false
    abortRef.current?.abort()
    abortRef.current = null

    setStatus('connecting')

    // Build the source
    const make =
      factoryRef.current ??
      ((url: string) =>
        new EventSource(url, { withCredentials: true }) as unknown as EventSourceLike)
    const url = buildTailUrl(expr, services)
    const src = make(url)
    sourceRef.current = src

    // Register 'open' listener
    src.addEventListener('open', () => {
      if (sourceRef.current !== src) return
      openedRef.current = true
      setStatus('open')
      failureCountRef.current = 0
      setFailureCount(0)
    })

    // 'line' listener
    src.addEventListener('line', (raw: Event) => {
      if (sourceRef.current !== src) return
      const ev = raw as MessageEvent<string>
      let parsed: LogLine | null = null
      try {
        parsed = JSON.parse(ev.data) as LogLine
      } catch {
        // parsed stays null
      }
      if (parsed === null) return
      bufferRef.current.push(parsed)
      if (bufferRef.current.length > cap) {
        bufferRef.current = bufferRef.current.slice(bufferRef.current.length - cap)
      }
      scheduleFlush()
    })

    // 'dropped' listener
    src.addEventListener('dropped', (raw: Event) => {
      if (sourceRef.current !== src) return
      const ev = raw as MessageEvent<string>
      try {
        const obj = JSON.parse(ev.data) as { count?: number }
        if (typeof obj.count === 'number') {
          droppedRef.current += obj.count
        }
      } catch {
        /* ignore malformed */
      }
    })

    // Failure classification helper
    async function classifyFailure() {
      const url = buildTailUrl(expr, services)
      const doFetch = fetchImplRef.current ?? fetch
      const controller = new AbortController()
      abortRef.current = controller
      let res: Response
      try {
        res = await doFetch(url, {
          credentials: 'same-origin',
          signal: controller.signal,
        })
      } catch {
        // Network error during classification
        if (sourceRef.current !== null) return
        failureCountRef.current += 1
        setFailureCount(failureCountRef.current)
        setStatus('error')
        return
      } finally {
        // Always release the connection ASAP
        controller.abort()
        if (abortRef.current === controller) abortRef.current = null
      }

      const statusCode = res.status
      if (statusCode === 503) {
        const raHeader = res.headers.get('Retry-After')
        const ra = raHeader !== null ? Number(raHeader) : NaN
        setError({
          code: 'over_cap',
          message: 'Tail capacity reached. Try again shortly.',
          ...(Number.isFinite(ra) && { retryAfter: ra }),
        })
        setStatus('error')
        return
      }

      if (statusCode === 422) {
        setError({ code: 'invalid_logsql', message: 'Invalid LogsQL query.' })
        setStatus('error')
        return
      }

      if (statusCode === 502) {
        setError({ code: 'upstream_unavailable', message: 'Log backend unavailable.' })
        failureCountRef.current += 1
        setFailureCount(failureCountRef.current)
        setStatus('error')
        return
      }

      // 200 (or any other 2xx): transient error, backoff reconnect
      failureCountRef.current += 1
      setFailureCount(failureCountRef.current)
      setStatus('error')
    }

    // Transport error handler
    function handleTransportError(src: EventSourceLike) {
      if (sourceRef.current !== src) return
      src.close()
      sourceRef.current = null

      if (!openedRef.current && !classifyDoneRef.current) {
        classifyDoneRef.current = true
        void classifyFailure()
        return
      }

      // Was previously open or classification done: plain backoff
      failureCountRef.current += 1
      setFailureCount(failureCountRef.current)
      setStatus('error')
    }

    // 'error' listener (both SSE error and transport)
    src.addEventListener('error', (raw: Event) => {
      if (sourceRef.current !== src) return
      const data = (raw as MessageEvent<string>).data
      if (typeof data === 'string' && data.length > 0) {
        // SERVER-SENT SSE error message
        try {
          const obj = JSON.parse(data) as { code?: string; message?: string }
          setError({
            code: typeof obj.code === 'string' ? obj.code : 'tail_error',
            message: typeof obj.message === 'string' ? obj.message : 'Tail error',
          })
        } catch {
          setError({ code: 'tail_error', message: 'Tail error' })
        }
        return
      }

      // TRANSPORT failure (no .data)
      handleTransportError(src)
    })
  }, [expr, services, cap, scheduleFlush])

  // Backoff reconnect effect
  useEffect(() => {
    if (!opts.enabled) return
    if (status !== 'error' || failureCount === 0) return
    const delay = Math.min(INITIAL_BACKOFF_MS * 2 ** (failureCount - 1), MAX_BACKOFF_MS)
    const timer = setTimeout(() => {
      timerRef.current = null
      open()
    }, delay)
    timerRef.current = timer
    return () => {
      clearTimeout(timer)
      if (timerRef.current === timer) timerRef.current = null
    }
  }, [opts.enabled, status, failureCount, open])

  // Teardown helper
  const teardown = useCallback(() => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current)
      timerRef.current = null
    }
    cancelFlush()
    if (sourceRef.current !== null) {
      sourceRef.current.close()
      sourceRef.current = null
    }
    abortRef.current?.abort()
    abortRef.current = null
    bufferRef.current = []
    droppedRef.current = 0
    failureCountRef.current = 0
    openedRef.current = false
    classifyDoneRef.current = false
    setStatus('idle')
    setError(null)
    setFailureCount(0)
  }, [cancelFlush])

  // Mount / enabled effect
  useEffect(() => {
    if (!opts.enabled) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- intentional: teardown() resets EventSource-derived state when the live-tail subscription is disabled; the same imperative cleanup (close source, clear timers/AbortController, reset refs) must run in the effect body and cannot be deferred to a callback without flashing stale state. Mirrors src/lib/sse.ts:113.
      teardown()
      return
    }
    open()
    return () => {
      teardown()
    }
  }, [opts.enabled, open, teardown])

  // reconnect
  const reconnect = useCallback(() => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current)
      timerRef.current = null
    }
    failureCountRef.current = 0
    setFailureCount(0)
    setError(null)
    open()
  }, [open])

  return { status, error, reconnect }
}
