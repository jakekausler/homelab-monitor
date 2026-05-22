import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { ContainerGridCard } from './ContainerGridCard'
import type { ContainerRow } from './types'

afterEach(() => {
  cleanup()
})

describe('ContainerGridCard', () => {
  it('shows empty state when containers is empty', () => {
    render(<ContainerGridCard containers={[]} />)
    const mobile = screen.getByTestId('containers-mobile')
    expect(mobile).toHaveTextContent('No containers discovered yet.')
  })

  it('renders compose basename when compose_file_path is set', () => {
    const containers: ContainerRow[] = [
      {
        id: 'test-123',
        name: 'my-container',
        compose_file_path: '/storage/programs/homelab-monitor/deploy/compose/docker-compose.yml',
        labels: {},
      },
    ]
    render(<ContainerGridCard containers={containers} />)
    expect(screen.getByText('Compose:', { exact: false })).toHaveTextContent('compose')
  })

  it('renders dash for compose when compose_file_path is null', () => {
    const containers: ContainerRow[] = [
      {
        id: 'test-123',
        name: 'my-container',
        compose_file_path: null,
        labels: {},
      },
    ]
    render(<ContainerGridCard containers={containers} />)
    const composeDiv = screen.getByText('Compose:', { exact: false })
    expect(composeDiv).toHaveTextContent('—')
  })

  it('shows full compose_file_path as tooltip', () => {
    const filePath = '/storage/programs/homelab-monitor/deploy/compose/docker-compose.yml'
    const containers: ContainerRow[] = [
      {
        id: 'test-123',
        name: 'my-container',
        compose_file_path: filePath,
        labels: {},
      },
    ]
    render(<ContainerGridCard containers={containers} />)
    const composeDivs = screen.getAllByText('Compose:', { exact: false })
    expect(composeDivs[0]).toBeDefined()
    const composeDiv = composeDivs[0]
    expect(composeDiv).toHaveAttribute('title', filePath)
  })

  it('renders restart_count_24h when present and > 0', () => {
    const containers: ContainerRow[] = [
      {
        id: 'test-123',
        name: 'my-container',
        restart_count: 5,
        restart_count_24h: 3,
        labels: {},
      },
    ]
    render(<ContainerGridCard containers={containers} />)
    expect(screen.getByText('Restarts (24h):', { exact: false })).toBeInTheDocument()
    const restartDiv = screen.getByText('Restarts (24h):', { exact: false })
    expect(restartDiv).toHaveTextContent('3')
  })

  it('renders dash for restart_count_24h when 0', () => {
    const containers: ContainerRow[] = [
      {
        id: 'test-123',
        name: 'my-container',
        restart_count: 5,
        restart_count_24h: 0,
        labels: {},
      },
    ]
    render(<ContainerGridCard containers={containers} />)
    const restartDiv = screen.getByText('Restarts (24h):', { exact: false })
    expect(restartDiv).toHaveTextContent('—')
  })

  it('renders dash for restart_count_24h when null', () => {
    const containers: ContainerRow[] = [
      {
        id: 'test-123',
        name: 'my-container',
        restart_count: 5,
        restart_count_24h: null,
        labels: {},
      },
    ]
    render(<ContainerGridCard containers={containers} />)
    const restartDiv = screen.getByText('Restarts (24h):', { exact: false })
    expect(restartDiv).toHaveTextContent('—')
  })

  it('shows cumulative restart_count as tooltip', () => {
    const containers: ContainerRow[] = [
      {
        id: 'test-123',
        name: 'my-container',
        restart_count: 7,
        restart_count_24h: 2,
        labels: {},
      },
    ]
    render(<ContainerGridCard containers={containers} />)
    const restartDiv = screen.getByText('Restarts (24h):', { exact: false })
    expect(restartDiv).toHaveAttribute('title', 'Cumulative: 7')
  })

  it('renders compose basename from nested path correctly', () => {
    const containers: ContainerRow[] = [
      {
        id: 'test-123',
        name: 'my-container',
        compose_file_path: '/a/b/c/docker-compose.yml',
        labels: {},
      },
    ]
    render(<ContainerGridCard containers={containers} />)
    expect(screen.getByText(/c$/)).toBeInTheDocument()
  })

  it('renders multiple containers', () => {
    const containers: ContainerRow[] = [
      {
        id: 'test-123',
        name: 'nginx',
        compose_file_path: '/a/b/compose/docker-compose.yml',
        labels: {},
      },
      {
        id: 'test-456',
        name: 'postgres',
        compose_file_path: '/x/y/compose/docker-compose.yml',
        labels: {},
      },
    ]
    render(<ContainerGridCard containers={containers} />)
    expect(screen.getByText('nginx')).toBeInTheDocument()
    expect(screen.getByText('postgres')).toBeInTheDocument()
  })

  it('renders container name', () => {
    const containers: ContainerRow[] = [
      {
        id: 'test-123',
        name: 'my-service',
        labels: {},
      },
    ]
    render(<ContainerGridCard containers={containers} />)
    expect(screen.getByText('my-service')).toBeInTheDocument()
  })
})
