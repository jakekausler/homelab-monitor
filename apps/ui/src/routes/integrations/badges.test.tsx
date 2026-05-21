import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { StatusBadge, HealthcheckBadge, RestartCountBadge } from './badges'

afterEach(cleanup)

describe('StatusBadge', () => {
  it.each([
    ['running', 'Running'],
    ['exited', 'Exited'],
    ['restarting', 'Restarting'],
    ['paused', 'Paused'],
    ['dead', 'Dead'],
    ['missing', 'Missing'],
  ])('renders %s with title-cased text %s', (status, label) => {
    render(<StatusBadge status={status} />)
    expect(screen.getByText(label)).toBeInTheDocument()
    expect(screen.getByLabelText(`Container status ${status}`)).toBeInTheDocument()
  })

  it('falls back to muted variant for unknown status', () => {
    render(<StatusBadge status="weird" />)
    expect(screen.getByText('Weird')).toBeInTheDocument()
  })
})

describe('HealthcheckBadge', () => {
  it.each([
    ['healthy', 'Healthy'],
    ['unhealthy', 'Unhealthy'],
    ['starting', 'Starting'],
  ])('renders %s', (status, label) => {
    render(<HealthcheckBadge status={status} />)
    expect(screen.getByText(label)).toBeInTheDocument()
    expect(screen.getByLabelText(`Healthcheck ${status}`)).toBeInTheDocument()
  })
})

describe('RestartCountBadge', () => {
  it('renders 0 as plain muted span', () => {
    render(<RestartCountBadge count={0} />)
    expect(screen.getByText('0')).toBeInTheDocument()
  })

  it('renders 1 with warn variant', () => {
    render(<RestartCountBadge count={1} />)
    expect(screen.getByLabelText('Restart count 1')).toBeInTheDocument()
  })

  it('renders 5 with critical variant (>= 3 threshold)', () => {
    render(<RestartCountBadge count={5} />)
    expect(screen.getByLabelText('Restart count 5')).toBeInTheDocument()
  })
})
