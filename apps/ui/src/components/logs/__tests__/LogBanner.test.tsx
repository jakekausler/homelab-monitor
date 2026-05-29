import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { LogBanner } from '@/components/logs/LogBanner'

afterEach(cleanup)

describe('LogBanner', () => {
  it('renders amber tone classes', () => {
    render(
      <LogBanner tone="amber" testId="b">
        hi
      </LogBanner>,
    )

    const banner = screen.getByTestId('b')
    expect(banner.className).toContain('border-amber-500/40')
    expect(banner.className).toContain('bg-amber-500/10')
    expect(banner.className).toContain('p-2')
    expect(banner.className).toContain('text-xs')
  })

  it('renders blue tone classes', () => {
    render(
      <LogBanner tone="blue" testId="b">
        hi
      </LogBanner>,
    )

    const banner = screen.getByTestId('b')
    expect(banner.className).toContain('border-blue-500/30')
    expect(banner.className).toContain('bg-blue-500/10')
  })

  it('applies role attribute when provided', () => {
    render(
      <LogBanner tone="amber" role="status" testId="b">
        hi
      </LogBanner>,
    )

    expect(screen.getByTestId('b')).toHaveAttribute('role', 'status')
  })

  it('does not apply role when absent', () => {
    render(
      <LogBanner tone="amber" testId="b">
        hi
      </LogBanner>,
    )

    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('renders children', () => {
    render(
      <LogBanner tone="amber" testId="b">
        hi
      </LogBanner>,
    )

    expect(screen.getByText('hi')).toBeInTheDocument()
  })
})
