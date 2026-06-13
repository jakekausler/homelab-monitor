import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { HaEntityHealthWidget } from './HaEntityHealthWidget'

afterEach(() => {
  cleanup()
})

describe('HaEntityHealthWidget', () => {
  it('renders all three counts', () => {
    render(<HaEntityHealthWidget entities={{ total: 1906, available: 943, unavailable: 963 }} />)
    expect(screen.getByText('1906')).toBeInTheDocument()
    expect(screen.getByText('943')).toBeInTheDocument()
    expect(screen.getByText('963')).toBeInTheDocument()
  })

  it('tints unavailable amber when > 0', () => {
    render(<HaEntityHealthWidget entities={{ total: 1906, available: 943, unavailable: 963 }} />)
    const unavailableDD = screen.getByText('963')
    expect(unavailableDD.className).toContain('text-amber-700')
  })

  it('does not tint unavailable when 0', () => {
    render(<HaEntityHealthWidget entities={{ total: 100, available: 100, unavailable: 0 }} />)
    const unavailableDD = screen.getByText('0')
    expect(unavailableDD.className).not.toContain('text-amber-700')
  })
})
