import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { LogLine } from '@/components/logs/types'
import { useLogsTail, type EventSourceLike } from './logsTail'

let fakeInstances: FakeEventSource[] = []

class FakeEventSource implements EventSourceLike {
  url: string
  listeners: Record<string, ((event: Event) => void)[]> = {}
  closed = false
  onopen: ((this: EventSourceLike, ev: Event) => unknown) | null = null
  onerror: ((this: EventSourceLike, ev: Event) => unknown) | null = null

  constructor(url: string) {
    this.url = url
    fakeInstances.push(this)
  }

  addEventListener(name: string, fn: (event: Event) => void) {
    const list = this.listeners[name] ?? []
    list.push(fn)
    this.listeners[name] = list
  }

  emit(name: string, init: { data?: string } = {}) {
    const fns = this.listeners[name] ?? []
    const ev =
      init.data === undefined ? new Event(name) : new MessageEvent(name, { data: init.data })
    for (const fn of fns) fn(ev)
  }

  close() {
    this.closed = true
  }
}

beforeEach(() => {
  fakeInstances = []
  vi.useFakeTimers()
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback): number => {
    cb(0)
    return 1
  })
  vi.stubGlobal('cancelAnimationFrame', (_id: number): void => {})
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

function fakeResponse(status: number, retryAfter?: string): Response {
  return {
    status,
    headers: { get: (k: string) => (k === 'Retry-After' ? (retryAfter ?? null) : null) },
  } as unknown as Response
}

function makeFetch(impl: () => Promise<Response> | Response): typeof fetch {
  return vi.fn(impl) as unknown as typeof fetch
}

function renderTail(
  opts: Partial<Parameters<typeof useLogsTail>[2]> & { enabled: boolean },
  expr = '*',
  services = '',
) {
  return renderHook(() =>
    useLogsTail(expr, services, {
      factory: (url) => new FakeEventSource(url),
      ...opts,
    }),
  )
}

function line(message: string): string {
  return JSON.stringify({
    timestamp: '2026-06-05T12:00:00Z',
    message,
    stream: 'stdout',
    severity: null,
    host: null,
    service: null,
    fields: {},
  })
}

describe('useLogsTail', () => {
  it('enabled=false → idle, no source', () => {
    const { result } = renderTail({ enabled: false })
    expect(result.current.status).toBe('idle')
    expect(fakeInstances.length).toBe(0)
  })

  it('enabled=true opens an EventSource and goes connecting→open', () => {
    const { result } = renderTail({ enabled: true })
    expect(result.current.status).toBe('connecting')
    expect(fakeInstances.length).toBe(1)
    const src = fakeInstances[0]
    if (src === undefined) throw new Error('no source')
    act(() => {
      src.emit('open')
    })
    expect(result.current.status).toBe('open')
  })

  it('builds URL with services when non-empty', () => {
    renderTail({ enabled: true }, 'foo bar', 'docker:nginx')
    const inst = fakeInstances[0]
    if (inst === undefined) throw new Error('no source')
    const url = inst.url
    expect(url).toContain('expr=')
    expect(url).toContain('services=')
  })

  it('omits services param when empty', () => {
    renderTail({ enabled: true }, '*', '')
    const inst2 = fakeInstances[0]
    if (inst2 === undefined) throw new Error('no source')
    const url = inst2.url
    expect(url).toBe('/api/logs/tail?expr=*')
    expect(url).not.toContain('services=')
  })

  it("SSE 'error' message event sets structured error (NOT transport)", () => {
    const { result } = renderTail({ enabled: true })
    const src = fakeInstances[0]
    if (src === undefined) throw new Error('no source')
    act(() => {
      src.emit('open')
      src.emit('error', {
        data: '{"code":"invalid_logsql","message":"bad"}',
      })
    })
    expect(result.current.error?.code).toBe('invalid_logsql')
    expect(result.current.error?.message).toBe('bad')
    expect(src.closed).toBe(false)
    expect(fakeInstances.length).toBe(1)
  })

  it("SSE 'error' with malformed data falls back to generic code", () => {
    const { result } = renderTail({ enabled: true })
    const src = fakeInstances[0]
    if (src === undefined) throw new Error('no source')
    act(() => {
      src.emit('open')
      src.emit('error', { data: '{' })
    })
    expect(result.current.error?.code).toBe('tail_error')
  })

  it('transport error before open → classifies, increments failure, backoff reconnect (200 path)', async () => {
    const { result } = renderTail({
      enabled: true,
      fetchImpl: makeFetch(() => fakeResponse(200)),
    })
    const src = fakeInstances[0]
    if (src === undefined) throw new Error('no source')
    await act(async () => {
      src.emit('error')
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(src.closed).toBe(true)
    expect(result.current.status).toBe('error')
    await act(async () => {
      vi.advanceTimersByTime(500)
      await Promise.resolve()
    })
    expect(fakeInstances.length).toBe(2)
  })

  it('classification 503 → over_cap + retryAfter, NO reconnect', async () => {
    const fetchSpy = makeFetch(() => fakeResponse(503, '60'))
    const { result } = renderTail({
      enabled: true,
      fetchImpl: fetchSpy,
    })
    const src = fakeInstances[0]
    if (src === undefined) throw new Error('no source')
    await act(async () => {
      src.emit('error')
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(result.current.error?.code).toBe('over_cap')
    expect(result.current.error?.retryAfter).toBe(60)
    expect(result.current.status).toBe('error')
    act(() => {
      vi.advanceTimersByTime(1000)
    })
    expect(fakeInstances.length).toBe(1)
  })

  it('classification 503 without Retry-After → over_cap, retryAfter undefined', async () => {
    const { result } = renderTail({
      enabled: true,
      fetchImpl: makeFetch(() => fakeResponse(503)),
    })
    const src = fakeInstances[0]
    if (src === undefined) throw new Error('no source')
    await act(async () => {
      src.emit('error')
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(result.current.error?.code).toBe('over_cap')
    expect(result.current.error?.retryAfter).toBeUndefined()
  })

  it('classification 422 → invalid_logsql, STOP (no reconnect)', async () => {
    const { result } = renderTail({
      enabled: true,
      fetchImpl: makeFetch(() => fakeResponse(422)),
    })
    const src = fakeInstances[0]
    if (src === undefined) throw new Error('no source')
    await act(async () => {
      src.emit('error')
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(result.current.error?.code).toBe('invalid_logsql')
    act(() => {
      vi.advanceTimersByTime(1000)
    })
    expect(fakeInstances.length).toBe(1)
  })

  it('classification 502 → upstream_unavailable, reconnects with backoff', async () => {
    const { result } = renderTail({
      enabled: true,
      fetchImpl: makeFetch(() => fakeResponse(502)),
    })
    const src = fakeInstances[0]
    if (src === undefined) throw new Error('no source')
    await act(async () => {
      src.emit('error')
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(result.current.error?.code).toBe('upstream_unavailable')
    act(() => {
      vi.advanceTimersByTime(500)
    })
    expect(fakeInstances.length).toBe(2)
  })

  it('classification network error → generic error + reconnect', async () => {
    const { result } = renderTail({
      enabled: true,
      fetchImpl: makeFetch(() => Promise.reject(new Error('net'))),
    })
    const src = fakeInstances[0]
    if (src === undefined) throw new Error('no source')
    await act(async () => {
      src.emit('error')
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(result.current.status).toBe('error')
    act(() => {
      vi.advanceTimersByTime(500)
    })
    expect(fakeInstances.length).toBe(2)
  })

  it('transport error AFTER a successful open → plain backoff (no classification)', async () => {
    const fetchSpy = makeFetch(() => fakeResponse(200))
    const { result } = renderTail({
      enabled: true,
      fetchImpl: fetchSpy,
    })
    const src = fakeInstances[0]
    if (src === undefined) throw new Error('no source')
    await act(async () => {
      src.emit('open')
      src.emit('error')
      await Promise.resolve()
    })
    expect(result.current.status).toBe('error')
    expect(src.closed).toBe(true)
    expect(fetchSpy).not.toHaveBeenCalled()
    act(() => {
      vi.advanceTimersByTime(500)
    })
    expect(fakeInstances.length).toBe(2)
  })

  it('reconnect() clears pending timer, resets failureCount, reopens', async () => {
    const { result } = renderTail({
      enabled: true,
      fetchImpl: makeFetch(() => fakeResponse(502)),
    })
    const src = fakeInstances[0]
    if (src === undefined) throw new Error('no source')
    // Trigger error to schedule backoff
    await act(async () => {
      src.emit('error')
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(result.current.status).toBe('error')

    // Call reconnect
    act(() => {
      result.current.reconnect()
    })
    expect(result.current.status).toBe('connecting') // reconnect reopened: failureCount reset + new source connecting
    expect(fakeInstances.length).toBe(2)

    // Old timer should not fire
    act(() => {
      vi.advanceTimersByTime(500)
    })
    expect(fakeInstances.length).toBe(2) // no extra open
  })

  it('enabled true→false tears down (source closed, state reset)', () => {
    const { result, rerender } = renderHook(
      ({ enabled }) =>
        useLogsTail('*', '', { factory: (url) => new FakeEventSource(url), enabled }),
      { initialProps: { enabled: true } },
    )
    const src = fakeInstances[0]
    if (src === undefined) throw new Error('no source')
    act(() => {
      src.emit('open')
    })
    expect(result.current.status).toBe('open')

    act(() => {
      rerender({ enabled: false })
    })
    expect(src.closed).toBe(true)
    expect(result.current.status).toBe('idle')
  })

  it('unmount closes the source and clears timers', async () => {
    const { unmount } = renderTail({
      enabled: true,
      fetchImpl: makeFetch(() => fakeResponse(502)),
    })
    const src = fakeInstances[0]
    if (src === undefined) throw new Error('no source')
    await act(async () => {
      src.emit('error')
      await Promise.resolve()
      await Promise.resolve()
    })
    const beforeUnmount = fakeInstances.length

    unmount()
    act(() => {
      vi.advanceTimersByTime(1000)
    })
    expect(fakeInstances.length).toBe(beforeUnmount) // no new instances
    expect(src.closed).toBe(true)
  })

  it('default factory path (no factory) uses global EventSource', () => {
    const originalES = globalThis.EventSource
    try {
      vi.stubGlobal('EventSource', FakeEventSource)
      const { result } = renderTail({ enabled: true }, '*', '')
      expect(fakeInstances.length).toBeGreaterThanOrEqual(1)
      expect(result.current.status).toBe('connecting')
    } finally {
      globalThis.EventSource = originalES
    }
  })

  it('onLines is called with rAF-batched parsed LogLines', () => {
    const onLines = vi.fn<(batch: LogLine[]) => void>()
    renderTail({ enabled: true, onLines })
    const src = fakeInstances[0]
    if (src === undefined) throw new Error('no source')
    act(() => {
      src.emit('open')
      src.emit('line', { data: line('a') })
      src.emit('line', { data: line('b') })
    })
    expect(onLines).toHaveBeenCalled()
    const allBatches = onLines.mock.calls.flatMap((c) => c[0])
    const messages = allBatches.map((l) => l.message)
    expect(messages).toEqual(['a', 'b'])
  })

  it('onLines NOT called for malformed line JSON', () => {
    const onLines = vi.fn<(batch: LogLine[]) => void>()
    renderTail({ enabled: true, onLines })
    const src = fakeInstances[0]
    if (src === undefined) throw new Error('no source')
    act(() => {
      src.emit('open')
      src.emit('line', { data: 'not json' })
    })
    expect(onLines).not.toHaveBeenCalled()
  })

  it('onLines not provided → no crash', () => {
    const { result } = renderTail({ enabled: true })
    const src = fakeInstances[0]
    if (src === undefined) throw new Error('no source')
    expect(() => {
      act(() => {
        src.emit('open')
        src.emit('line', { data: line('a') })
      })
    }).not.toThrow()
    expect(result.current.status).toBe('open')
  })
})
