import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { ErrorDisplay } from '@/components/ErrorDisplay'

afterEach(() => {
  cleanup()
})

describe('ErrorDisplay', () => {
  it('renders error.message when given an Error instance', () => {
    render(<ErrorDisplay error={new Error('boom')} />)
    expect(screen.getByText('boom')).toBeInTheDocument()
  })

  it('renders String(error.message) when given a plain object with message', () => {
    render(<ErrorDisplay error={{ code: 'x', message: 'plain object failed' }} />)
    expect(screen.getByText('plain object failed')).toBeInTheDocument()
  })

  it('renders fallback when given an object without message', () => {
    render(<ErrorDisplay error={{ code: 'x' }} />)
    expect(screen.getByText('An unexpected error occurred')).toBeInTheDocument()
  })

  it('renders fallback when given null', () => {
    render(<ErrorDisplay error={null} />)
    expect(screen.getByText('An unexpected error occurred')).toBeInTheDocument()
  })
})
