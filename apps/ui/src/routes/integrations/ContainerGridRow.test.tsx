import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

import { ContainerGridRow } from './ContainerGridRow'
import type { ContainerRow } from './types'

afterEach(() => {
  cleanup()
})

function renderWithQueryClient(ui: ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  })
  return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>)
}

describe('ContainerGridRow', () => {
  it('renders compose basename when compose_file_path is set', () => {
    const container: ContainerRow = {
      id: 'test-123',
      name: 'my-container',
      compose_file_path: '/storage/programs/homelab-monitor/deploy/compose/docker-compose.yml',
      labels: {},
    }
    renderWithQueryClient(
      <table>
        <tbody>
          <ContainerGridRow container={container} />
        </tbody>
      </table>,
    )
    expect(screen.getByText('compose')).toBeInTheDocument()
  })

  it('renders dash when compose_file_path is null', () => {
    const container: ContainerRow = {
      id: 'test-123',
      name: 'my-container',
      compose_file_path: null,
      labels: {},
    }
    renderWithQueryClient(
      <table>
        <tbody>
          <ContainerGridRow container={container} />
        </tbody>
      </table>,
    )
    // The dash should be in the compose cell (first td after tr)
    const cells = screen.getAllByRole('cell')
    expect(cells[0]).toHaveTextContent('—')
  })

  it('shows full compose_file_path as tooltip', () => {
    const filePath = '/storage/programs/homelab-monitor/deploy/compose/docker-compose.yml'
    const container: ContainerRow = {
      id: 'test-123',
      name: 'my-container',
      compose_file_path: filePath,
      labels: {},
    }
    renderWithQueryClient(
      <table>
        <tbody>
          <ContainerGridRow container={container} />
        </tbody>
      </table>,
    )
    const composeCells = screen.getAllByRole('cell')
    expect(composeCells[0]).toHaveAttribute('title', filePath)
  })

  it('renders restart_count_24h when present and > 0', () => {
    const container: ContainerRow = {
      id: 'test-123',
      name: 'my-container',
      restart_count: 5,
      restart_count_24h: 3,
      labels: {},
    }
    renderWithQueryClient(
      <table>
        <tbody>
          <ContainerGridRow container={container} />
        </tbody>
      </table>,
    )
    expect(screen.getByText('3')).toBeInTheDocument()
  })

  it('renders dash for restart_count_24h when 0', () => {
    const container: ContainerRow = {
      id: 'test-123',
      name: 'my-container',
      restart_count: 5,
      restart_count_24h: 0,
      labels: {},
    }
    renderWithQueryClient(
      <table>
        <tbody>
          <ContainerGridRow container={container} />
        </tbody>
      </table>,
    )
    const cells = screen.getAllByRole('cell')
    // Restarts (24h) cell is at index 3 (after compose, name, status)
    expect(cells[3]).toHaveTextContent('—')
  })

  it('renders dash for restart_count_24h when null', () => {
    const container: ContainerRow = {
      id: 'test-123',
      name: 'my-container',
      restart_count: 5,
      restart_count_24h: null,
      labels: {},
    }
    renderWithQueryClient(
      <table>
        <tbody>
          <ContainerGridRow container={container} />
        </tbody>
      </table>,
    )
    const cells = screen.getAllByRole('cell')
    expect(cells[3]).toHaveTextContent('—')
  })

  it('shows cumulative restart_count as tooltip for restart_count_24h', () => {
    const container: ContainerRow = {
      id: 'test-123',
      name: 'my-container',
      restart_count: 7,
      restart_count_24h: 2,
      labels: {},
    }
    renderWithQueryClient(
      <table>
        <tbody>
          <ContainerGridRow container={container} />
        </tbody>
      </table>,
    )
    const cells = screen.getAllByRole('cell')
    expect(cells[3]).toHaveAttribute('title', 'Cumulative: 7')
  })

  it('renders compose basename from nested path correctly', () => {
    const container: ContainerRow = {
      id: 'test-123',
      name: 'my-container',
      compose_file_path: '/a/b/c/docker-compose.yml',
      labels: {},
    }
    renderWithQueryClient(
      <table>
        <tbody>
          <ContainerGridRow container={container} />
        </tbody>
      </table>,
    )
    expect(screen.getByText('c')).toBeInTheDocument()
  })

  it('renders container name', () => {
    const container: ContainerRow = {
      id: 'test-123',
      name: 'nginx',
      labels: {},
    }
    renderWithQueryClient(
      <table>
        <tbody>
          <ContainerGridRow container={container} />
        </tbody>
      </table>,
    )
    expect(screen.getByText('nginx')).toBeInTheDocument()
  })
})
