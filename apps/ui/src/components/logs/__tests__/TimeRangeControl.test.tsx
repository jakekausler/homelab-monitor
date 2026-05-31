import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { TimeRangeControl } from '@/components/logs/TimeRangeControl'
import type { TimeRangeValue } from '@/lib/timeRange'

afterEach(cleanup)

// matchMedia mock helper — mirrors the pattern in apps/ui/src/lib/__tests__/useMediaQuery.test.ts
function installMatchMedia(initial: boolean): void {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    configurable: true,
    value: (query: string) => ({
      matches: initial,
      media: query,
      onchange: null,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      addListener: () => undefined,
      removeListener: () => undefined,
      dispatchEvent: () => false,
    }),
  })
}

const PRESET_VALUE: TimeRangeValue = { kind: 'preset', token: '15m' }

function openPanel() {
  fireEvent.click(screen.getByTestId('time-range-trigger'))
}

describe('TimeRangeControl (mobile/dialog path)', () => {
  it('shows the current preset label on the trigger', () => {
    render(<TimeRangeControl value={PRESET_VALUE} onChange={vi.fn()} />)
    expect(screen.getByTestId('time-range-trigger')).toHaveTextContent('Last 15m')
  })

  it('selecting a preset emits a preset value and closes', () => {
    const onChange = vi.fn()
    render(<TimeRangeControl value={PRESET_VALUE} onChange={onChange} />)
    openPanel()
    fireEvent.click(screen.getByTestId('preset-1h'))
    expect(onChange).toHaveBeenCalledWith({ kind: 'preset', token: '1h' })
  })

  it('opening custom + entering a valid range + Apply emits a custom value', () => {
    const onChange = vi.fn()
    render(<TimeRangeControl value={PRESET_VALUE} onChange={onChange} />)
    openPanel()
    fireEvent.click(screen.getByTestId('custom-range-toggle'))
    // Past dates (relative to real now) so the no-future rule passes.
    fireEvent.change(screen.getByTestId('custom-start'), {
      target: { value: '2020-01-01T00:00' },
    })
    fireEvent.change(screen.getByTestId('custom-end'), {
      target: { value: '2020-01-01T01:00' },
    })
    fireEvent.click(screen.getByTestId('custom-apply'))
    expect(onChange).toHaveBeenCalledTimes(1)
    const arg = onChange.mock.calls[0]![0] as TimeRangeValue
    expect(arg.kind).toBe('custom')
  })

  it('invalid range (start after end) shows error and does NOT emit', () => {
    const onChange = vi.fn()
    render(<TimeRangeControl value={PRESET_VALUE} onChange={onChange} />)
    openPanel()
    fireEvent.click(screen.getByTestId('custom-range-toggle'))
    fireEvent.change(screen.getByTestId('custom-start'), {
      target: { value: '2020-01-02T00:00' },
    })
    fireEvent.change(screen.getByTestId('custom-end'), {
      target: { value: '2020-01-01T00:00' },
    })
    fireEvent.click(screen.getByTestId('custom-apply'))
    expect(screen.getByTestId('custom-range-error')).toBeInTheDocument()
    expect(onChange).not.toHaveBeenCalled()
  })

  it('future range shows error and does NOT emit', () => {
    const onChange = vi.fn()
    render(<TimeRangeControl value={PRESET_VALUE} onChange={onChange} />)
    openPanel()
    fireEvent.click(screen.getByTestId('custom-range-toggle'))
    fireEvent.change(screen.getByTestId('custom-start'), {
      target: { value: '2999-01-01T00:00' },
    })
    fireEvent.change(screen.getByTestId('custom-end'), {
      target: { value: '2999-01-01T01:00' },
    })
    fireEvent.click(screen.getByTestId('custom-apply'))
    expect(screen.getByTestId('custom-range-error')).toBeInTheDocument()
    expect(onChange).not.toHaveBeenCalled()
  })

  it('Cancel closes without emitting', () => {
    const onChange = vi.fn()
    render(<TimeRangeControl value={PRESET_VALUE} onChange={onChange} />)
    openPanel()
    fireEvent.click(screen.getByTestId('custom-range-toggle'))
    fireEvent.click(screen.getByTestId('custom-cancel'))
    expect(onChange).not.toHaveBeenCalled()
  })

  it('bounded mode rejects a range outside [min,max]', () => {
    const onChange = vi.fn()
    const min = new Date('2020-01-01T00:00:00Z')
    const max = new Date('2020-01-01T02:00:00Z')
    render(
      <TimeRangeControl
        value={{ kind: 'custom', start: min, end: max }}
        onChange={onChange}
        mode="bounded"
        min={min}
        max={max}
        presets={[]}
      />,
    )
    openPanel()
    // Start before min → bounded error.
    fireEvent.change(screen.getByTestId('custom-start'), {
      target: { value: '2019-12-31T00:00' },
    })
    fireEvent.change(screen.getByTestId('custom-end'), {
      target: { value: '2020-01-01T01:00' },
    })
    fireEvent.click(screen.getByTestId('custom-apply'))
    expect(screen.getByTestId('custom-range-error')).toBeInTheDocument()
    expect(onChange).not.toHaveBeenCalled()
  })

  it('bounded mode sets min/max attrs on the inputs', () => {
    const min = new Date('2020-01-01T00:00:00Z')
    const max = new Date('2020-01-01T02:00:00Z')
    render(
      <TimeRangeControl
        value={{ kind: 'custom', start: min, end: max }}
        onChange={vi.fn()}
        mode="bounded"
        min={min}
        max={max}
        presets={[]}
      />,
    )
    openPanel()
    const startInput = screen.getByTestId('custom-start')
    expect(startInput).toHaveAttribute('min')
    expect(startInput).toHaveAttribute('max')
  })

  it('open custom: only start filled, end empty → Apply emits {kind:custom, start:Date, end:undefined} and closes', () => {
    const onChange = vi.fn()
    render(<TimeRangeControl value={PRESET_VALUE} onChange={onChange} />)
    openPanel()
    fireEvent.click(screen.getByTestId('custom-range-toggle'))
    fireEvent.change(screen.getByTestId('custom-start'), {
      target: { value: '2020-01-01T00:00' },
    })
    // Leave end empty
    fireEvent.click(screen.getByTestId('custom-apply'))
    expect(onChange).toHaveBeenCalledTimes(1)
    const arg = onChange.mock.calls[0]![0] as TimeRangeValue
    expect(arg.kind).toBe('custom')
    if (arg.kind === 'custom') {
      expect(arg.start).toBeInstanceOf(Date)
      expect(arg.end).toBeUndefined()
    }
    // Panel closed: trigger should be visible again (no panel content)
    expect(screen.queryByTestId('custom-apply')).toBeNull()
  })

  it('open custom: both start and end empty → Apply emits {kind:custom, start:undefined, end:undefined}', () => {
    const onChange = vi.fn()
    render(<TimeRangeControl value={PRESET_VALUE} onChange={onChange} />)
    openPanel()
    fireEvent.click(screen.getByTestId('custom-range-toggle'))
    // Leave both inputs empty
    fireEvent.click(screen.getByTestId('custom-apply'))
    expect(onChange).toHaveBeenCalledTimes(1)
    const arg = onChange.mock.calls[0]![0] as TimeRangeValue
    expect(arg.kind).toBe('custom')
    if (arg.kind === 'custom') {
      expect(arg.start).toBeUndefined()
      expect(arg.end).toBeUndefined()
    }
  })
})

describe('TimeRangeControl (desktop/dropdown path)', () => {
  afterEach(() => {
    // Remove the matchMedia override so mobile tests keep their default (no matchMedia = false)
    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      configurable: true,
      value: undefined,
    })
  })

  it('desktop: opening the dropdown reveals preset buttons and the custom-range toggle', () => {
    installMatchMedia(true)
    render(<TimeRangeControl value={PRESET_VALUE} onChange={vi.fn()} />)
    // Radix DropdownMenu in jsdom requires a pointer-down + click sequence to open.
    // fireEvent.pointerDown dispatches the event radix listens to for open state.
    const trigger = screen.getByTestId('time-range-trigger')
    fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false })
    fireEvent.click(trigger)
    // The panel content renders inside the DropdownMenuContent portal.
    expect(screen.getByTestId('preset-15m')).toBeInTheDocument()
    expect(screen.getByTestId('preset-1h')).toBeInTheDocument()
    expect(screen.getByTestId('custom-range-toggle')).toBeInTheDocument()
  })
})
