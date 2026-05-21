import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { ContainerGrid } from './ContainerGrid'
import type { ContainerRow } from './types'

afterEach(() => {
  cleanup()
})

const MOCK_CONTAINERS: ContainerRow[] = [
  { id: 'abc123', name: 'nginx', labels: {} },
  { id: 'def456', name: 'postgres', status: 'running', image: 'postgres:16', labels: {} },
  {
    id: 'ghi789',
    name: 'redis',
    image: 'redis:7',
    status: 'running',
    cpu_pct: 0.42,
    mem_mib: 128,
    restart_count: 0,
    exit_code: null,
    healthcheck: 'healthy',
    image_update: 'available',
    probes: ['http'],
    logs_url: '/integrations/docker/containers/redis/logs',
    actions_available: true,
    labels: {},
  },
]

describe('ContainerGrid', () => {
  it('shows empty state when containers is empty', () => {
    render(<ContainerGrid containers={[]} />)
    const desktop = screen.getByTestId('containers-desktop')
    expect(desktop).toHaveTextContent('No containers discovered yet.')
    expect(desktop.querySelectorAll('tbody tr')).toHaveLength(1)
    const headers = [
      'Name',
      'Status',
      'Image',
      'CPU',
      'RAM',
      'Image Update',
      'Healthcheck',
      'Probes',
      'Logs',
      'Actions',
    ]
    for (const col of headers) {
      expect(desktop).toHaveTextContent(col)
    }
  })

  it('renders column headers when containers present', () => {
    render(<ContainerGrid containers={MOCK_CONTAINERS} />)
    const desktop = screen.getByTestId('containers-desktop')
    const headers = [
      'Name',
      'Status',
      'Image',
      'CPU',
      'RAM',
      'Image Update',
      'Healthcheck',
      'Probes',
      'Logs',
      'Actions',
    ]
    for (const col of headers) {
      expect(desktop).toHaveTextContent(col)
    }
  })

  it('renders a row per container', () => {
    render(<ContainerGrid containers={MOCK_CONTAINERS} />)
    const desktop = screen.getByTestId('containers-desktop')
    expect(desktop.querySelectorAll('tbody tr')).toHaveLength(3)
    expect(screen.getByText('nginx')).toBeInTheDocument()
    expect(screen.getByText('postgres')).toBeInTheDocument()
    expect(screen.getByText('redis')).toBeInTheDocument()
  })
})
