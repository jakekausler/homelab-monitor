import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/components/tiles/HostCpuTile', () => ({
  HostCpuTile: () => <div data-testid="host-cpu-tile" />,
}))

import { OverviewPage } from './Overview'

afterEach(() => {
  cleanup()
})

describe('OverviewPage', () => {
  it('renders the page heading', () => {
    render(<OverviewPage />)
    expect(screen.getByText('Overview')).toBeInTheDocument()
  })

  it('renders the HostCpuTile', () => {
    render(<OverviewPage />)
    expect(screen.getByTestId('host-cpu-tile')).toBeInTheDocument()
  })
})
