import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { HaUpdatesWidget } from './HaUpdatesWidget'

afterEach(() => {
  cleanup()
})

describe('HaUpdatesWidget', () => {
  it('renders the Available count when updates are available', () => {
    render(<HaUpdatesWidget updates={{ available: 5, total: 12 }} />)
    expect(screen.getByText('5')).toBeInTheDocument()
    expect(screen.getByText('Available')).toBeInTheDocument()
    expect(screen.queryByText('Total')).not.toBeInTheDocument()
  })

  it('tints Available amber when available > 0', () => {
    render(<HaUpdatesWidget updates={{ available: 5, total: 12 }} />)
    const availableValue = screen.getByText('5')
    expect(availableValue.className).toContain('text-amber-700')
  })

  it('renders EmptyState when available === 0', () => {
    render(<HaUpdatesWidget updates={{ available: 0, total: 12 }} />)
    expect(screen.getByText('All up to date')).toBeInTheDocument()
    expect(screen.queryByText('12')).not.toBeInTheDocument()
  })
})
