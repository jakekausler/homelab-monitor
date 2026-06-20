// SCAFFOLD TEST: validates honest empty-state placeholder only; real behavior tests arrive in STAGE-020/021
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

import { UnifiOverviewTab } from './UnifiOverviewTab'

afterEach(() => {
  cleanup()
})

describe('UnifiOverviewTab', () => {
  it('renders without crashing', () => {
    render(<UnifiOverviewTab />)
  })

  it('renders the honest placeholder text via role=status', () => {
    render(<UnifiOverviewTab />)
    const status = screen.getByRole('status')
    expect(status).toBeInTheDocument()
    expect(status).toHaveTextContent(
      'Unifi integration — coming in a later stage. No devices are being collected yet.',
    )
  })

  it('does not render any fake device/network/client data tables', () => {
    render(<UnifiOverviewTab />)
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(screen.queryByRole('grid')).not.toBeInTheDocument()
  })
})
