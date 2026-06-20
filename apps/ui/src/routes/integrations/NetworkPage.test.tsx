// SCAFFOLD TEST: validates honest empty-state placeholder only; real behavior tests arrive in STAGE-020/021
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

import { NetworkPage } from './NetworkPage'

afterEach(() => {
  cleanup()
})

describe('NetworkPage', () => {
  it('renders without crashing', () => {
    render(<NetworkPage />)
  })

  it('renders the page heading', () => {
    render(<NetworkPage />)
    expect(screen.getByRole('heading', { name: /^network$/i })).toBeInTheDocument()
  })

  it('renders the honest placeholder subtitle describing upcoming stages', () => {
    render(<NetworkPage />)
    expect(screen.getByText('Network monitoring lands in an upcoming stage.')).toBeInTheDocument()
  })

  it('renders the honest empty-state status with not-yet-configured copy', () => {
    render(<NetworkPage />)
    const status = screen.getByRole('status')
    expect(status).toBeInTheDocument()
    expect(status).toHaveTextContent('Network — not yet configured.')
  })

  it('does not render any fake network data tables', () => {
    render(<NetworkPage />)
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(screen.queryByRole('grid')).not.toBeInTheDocument()
  })
})
