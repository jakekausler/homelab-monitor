import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { HaBatteryWidget } from './HaBatteryWidget'

afterEach(() => {
  cleanup()
})

describe('HaBatteryWidget', () => {
  it('renders low and critical counts', () => {
    render(<HaBatteryWidget battery={{ low: 3, critical: 1 }} />)
    expect(screen.getByText('3')).toBeInTheDocument()
    expect(screen.getByText('1')).toBeInTheDocument()
  })

  it('tints critical red when > 0', () => {
    render(<HaBatteryWidget battery={{ low: 3, critical: 1 }} />)
    const criticalDD = screen.getByText('1')
    expect(criticalDD.className).toContain('text-red-700')
  })

  it('tints low amber when > 0', () => {
    render(<HaBatteryWidget battery={{ low: 3, critical: 1 }} />)
    const lowDD = screen.getByText('3')
    expect(lowDD.className).toContain('text-amber-700')
  })

  it('renders EmptyState when both zero', () => {
    render(<HaBatteryWidget battery={{ low: 0, critical: 0 }} />)
    expect(screen.getByText('All batteries healthy')).toBeInTheDocument()
    expect(screen.queryByRole('term')).not.toBeInTheDocument()
  })
})
