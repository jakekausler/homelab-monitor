import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

import { RunStateBadge, StateBadge } from '@/components/crons/badges'

afterEach(cleanup)

describe('StateBadge', () => {
  it('renders ok state', () => {
    render(<StateBadge state="ok" />)
    expect(screen.getByText('Ok')).toBeInTheDocument()
  })

  it('renders failed state with critical aria-label', () => {
    render(<StateBadge state="failed" />)
    expect(screen.getByLabelText('Last seen state failed')).toBeInTheDocument()
  })
})

describe('RunStateBadge', () => {
  it('renders running state with correct aria-label', () => {
    render(<RunStateBadge state="running" />)
    expect(screen.getByLabelText('Run state running')).toBeInTheDocument()
  })

  it('renders running state with text Running', () => {
    render(<RunStateBadge state="running" />)
    expect(screen.getByText('Running')).toBeInTheDocument()
  })

  it('renders ok state with text Ok', () => {
    render(<RunStateBadge state="ok" />)
    expect(screen.getByText('Ok')).toBeInTheDocument()
  })

  it('renders fail state with correct aria-label', () => {
    render(<RunStateBadge state="fail" />)
    expect(screen.getByLabelText('Run state fail')).toBeInTheDocument()
  })

  it('renders fail state with text Fail', () => {
    render(<RunStateBadge state="fail" />)
    expect(screen.getByText('Fail')).toBeInTheDocument()
  })

  it('renders unknown state with text Unknown', () => {
    render(<RunStateBadge state="unknown" />)
    expect(screen.getByText('Unknown')).toBeInTheDocument()
  })

  it('renders unknown state with correct aria-label', () => {
    render(<RunStateBadge state="unknown" />)
    expect(screen.getByLabelText('Run state unknown')).toBeInTheDocument()
  })
})
