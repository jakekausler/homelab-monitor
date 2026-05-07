import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useSSE } from './sse'

let fakeInstances: FakeEventSource[] = []

class FakeEventSource {
  url: string
  listeners: Record<string, ((event: Event) => void)[]> = {}
  closed = false

  constructor(url: string) {
    this.url = url
    fakeInstances.push(this)
  }

  addEventListener(name: string, fn: (event: Event) => void) {
    const list = this.listeners[name] ?? []
    list.push(fn)
    this.listeners[name] = list
  }

  emit(name: string, init: Partial<MessageEvent<string>> = {}) {
    const fns = this.listeners[name] ?? []
    const ev =
      name === 'open' || name === 'error'
        ? new Event(name)
        : new MessageEvent(name, { data: init.data ?? '' })
    for (const fn of fns) fn(ev)
  }

  close() {
    this.closed = true
  }
}

interface TickPayload {
  kind: 'collector.tick'
  collector: string
  ts: string
}

function parser(event: MessageEvent<string>): TickPayload | null {
  try {
    const obj: unknown = JSON.parse(event.data)
    if (typeof obj !== 'object' || obj === null) return null
    return obj as TickPayload
  } catch {
    return null
  }
}

beforeEach(() => {
  fakeInstances = []
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('useSSE', () => {
  it('opens an EventSource and surfaces parsed messages', () => {
    const { result } = renderHook(() =>
      useSSE<TickPayload>({
        topic: 'collector.tick',
        parser,
        factory: (url) => new FakeEventSource(url) as unknown as EventSource,
      }),
    )
    expect(result.current.status).toBe('connecting')
    const src = fakeInstances[0]
    expect(src).toBeDefined()
    if (src === undefined) throw new Error('no source')

    act(() => {
      src.emit('open')
    })
    expect(result.current.status).toBe('open')

    act(() => {
      src.emit('collector.tick', {
        data: JSON.stringify({
          kind: 'collector.tick',
          collector: 'host',
          ts: '2026-05-07T12:00:00Z',
        }),
      })
    })
    expect(result.current.value?.collector).toBe('host')
  })

  it('schedules a backoff reconnect on error', async () => {
    renderHook(() =>
      useSSE<TickPayload>({
        topic: 'collector.tick',
        parser,
        factory: (url) => new FakeEventSource(url) as unknown as EventSource,
      }),
    )
    const first = fakeInstances[0]
    if (first === undefined) throw new Error('no source')
    act(() => {
      first.emit('error')
    })
    expect(first.closed).toBe(true)
    await act(async () => {
      vi.advanceTimersByTime(500)
      await Promise.resolve()
    })
    expect(fakeInstances.length).toBe(2)
  })

  it('reconnect() resets failure counter and forces immediate reopen', () => {
    const { result } = renderHook(() =>
      useSSE<TickPayload>({
        topic: 'collector.tick',
        parser,
        factory: (url) => new FakeEventSource(url) as unknown as EventSource,
      }),
    )
    const first = fakeInstances[0]
    if (first === undefined) throw new Error('no source')
    act(() => {
      first.emit('error')
    })
    expect(result.current.failureCount).toBeGreaterThanOrEqual(1)
    act(() => {
      result.current.reconnect()
    })
    expect(result.current.failureCount).toBe(0)
    expect(fakeInstances.length).toBeGreaterThanOrEqual(2)
  })

  it('closes existing source and clears timer on unmount', () => {
    const { unmount } = renderHook(() =>
      useSSE<TickPayload>({
        topic: 'collector.tick',
        parser,
        factory: (url) => new FakeEventSource(url) as unknown as EventSource,
      }),
    )
    const first = fakeInstances[0]
    if (first === undefined) throw new Error('no source')

    // Emit error to schedule a backoff timer (failureCount becomes 1)
    act(() => {
      first.emit('error')
    })
    // A reconnect timer is now pending. Unmount should clear it and close the source.
    act(() => {
      unmount()
    })
    // Advance past the backoff window — no new source should open
    act(() => {
      vi.advanceTimersByTime(1000)
    })
    // Only the initial source was ever created
    expect(fakeInstances.length).toBe(1)
    expect(first.closed).toBe(true)
  })

  it('reconnect() clears a pending backoff timer before opening a new source', () => {
    const { result } = renderHook(() =>
      useSSE<TickPayload>({
        topic: 'collector.tick',
        parser,
        factory: (url) => new FakeEventSource(url) as unknown as EventSource,
      }),
    )
    const first = fakeInstances[0]
    if (first === undefined) throw new Error('no source')

    // Schedule a backoff timer
    act(() => {
      first.emit('error')
    })
    expect(fakeInstances.length).toBe(1)

    // Call reconnect() — this should clearTimeout the pending timer and open immediately
    act(() => {
      result.current.reconnect()
    })
    expect(fakeInstances.length).toBe(2)

    // Advance past the original backoff delay — the cleared timer must NOT fire again
    act(() => {
      vi.advanceTimersByTime(1000)
    })
    expect(fakeInstances.length).toBe(2)
  })

  it('manual reconnect short-circuits auto-backoff on the subsequent error', () => {
    const { result } = renderHook(() =>
      useSSE<TickPayload>({
        topic: 'collector.tick',
        parser,
        factory: (url) => new FakeEventSource(url) as unknown as EventSource,
      }),
    )
    const first = fakeInstances[0]
    if (first === undefined) throw new Error('no source')

    // Emit error on first source — backoff timer is scheduled
    act(() => {
      first.emit('error')
    })
    expect(result.current.failureCount).toBe(1)

    // Call reconnect() — sets manualReconnectRef, opens second source, clears old timer
    act(() => {
      result.current.reconnect()
    })
    const second = fakeInstances[1]
    if (second === undefined) throw new Error('no second source')
    expect(fakeInstances.length).toBe(2)

    // Emit error on the new source while manualReconnectRef is set — the error handler
    // should short-circuit (not increment failureCount, not schedule auto-backoff)
    act(() => {
      second.emit('error')
    })
    // failureCount stays at 0 because the short-circuit path skips the increment
    expect(result.current.failureCount).toBe(0)

    // Advance well past any backoff window — no third source should auto-open
    act(() => {
      vi.advanceTimersByTime(2000)
    })
    expect(fakeInstances.length).toBe(2)
  })

  it('does not schedule backoff when status is not error or failureCount is zero', () => {
    const { result } = renderHook(() =>
      useSSE<TickPayload>({
        topic: 'collector.tick',
        parser,
        factory: (url) => new FakeEventSource(url) as unknown as EventSource,
      }),
    )
    // On mount status is 'connecting' — the scheduling effect bail-out runs.
    // Then emit open to move to 'open' — still no backoff should schedule.
    const src = fakeInstances[0]
    if (src === undefined) throw new Error('no source')
    act(() => {
      src.emit('open')
    })
    expect(result.current.status).toBe('open')
    expect(result.current.failureCount).toBe(0)

    // Advance — no second source should appear
    act(() => {
      vi.advanceTimersByTime(1000)
    })
    expect(fakeInstances.length).toBe(1)
  })

  it('line 137: backoff cleanup sets timerRef to null only when it matches the scheduled timer', () => {
    // This test exercises the `if (timerRef.current === timer)` guard in the
    // backoff effect cleanup. We trigger two errors so two backoff effects run
    // in sequence; the cleanup of the first should not null-out the second's timer.
    const { result } = renderHook(() =>
      useSSE<TickPayload>({
        topic: 'collector.tick',
        parser,
        factory: (url) => new FakeEventSource(url) as unknown as EventSource,
      }),
    )
    const first = fakeInstances[0]
    if (first === undefined) throw new Error('no source')

    // First error — failureCount becomes 1, backoff timer T1 is scheduled.
    act(() => {
      first.emit('error')
    })
    expect(result.current.failureCount).toBe(1)

    // Advance past T1's delay (500 ms) → open() fires, second source created.
    act(() => {
      vi.advanceTimersByTime(500)
    })
    expect(fakeInstances.length).toBe(2)

    const second = fakeInstances[1]
    if (second === undefined) throw new Error('no second source')

    // Second error — failureCount becomes 2, backoff timer T2 is scheduled.
    act(() => {
      second.emit('error')
    })
    expect(result.current.failureCount).toBe(2)

    // Advance past T2's delay (1000 ms) → open() fires, third source created.
    act(() => {
      vi.advanceTimersByTime(1000)
    })
    expect(fakeInstances.length).toBe(3)
  })

  it('lines 65-66: open() closes an existing source before creating a new one', () => {
    // This exercises the `if (sourceRef.current !== null)` guard inside open().
    // We call reconnect() twice in a row — the second call must close the source
    // opened by the first call before opening a third.
    const { result } = renderHook(() =>
      useSSE<TickPayload>({
        topic: 'collector.tick',
        parser,
        factory: (url) => new FakeEventSource(url) as unknown as EventSource,
      }),
    )
    // Initial source is open (mount created instance[0])
    expect(fakeInstances.length).toBe(1)

    // First reconnect: closes instance[0], opens instance[1]
    act(() => {
      result.current.reconnect()
    })
    expect(fakeInstances[0]?.closed).toBe(true)
    expect(fakeInstances.length).toBe(2)

    // Second reconnect: closes instance[1], opens instance[2]
    act(() => {
      result.current.reconnect()
    })
    expect(fakeInstances[1]?.closed).toBe(true)
    expect(fakeInstances.length).toBe(3)
  })

  it('uses the default EventSource factory when no factory option is provided', () => {
    // Replace the global EventSource with FakeEventSource so the default factory
    // path (factoryRef.current ?? ((url) => new EventSource(url, ...))) is exercised.
    const OriginalEventSource = globalThis.EventSource
    globalThis.EventSource = FakeEventSource as unknown as typeof EventSource

    try {
      const { result } = renderHook(() =>
        useSSE<TickPayload>({
          topic: 'collector.tick',
          parser,
          // No factory — falls through to the global EventSource
        }),
      )
      // At least one FakeEventSource was created via the default factory path
      expect(fakeInstances.length).toBeGreaterThanOrEqual(1)
      expect(result.current.status).toBe('connecting')
    } finally {
      globalThis.EventSource = OriginalEventSource
    }
  })
})
