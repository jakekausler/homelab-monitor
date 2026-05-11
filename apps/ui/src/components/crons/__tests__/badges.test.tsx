import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'

import { ModeBadge, StateBadge } from '@/components/crons/badges'

describe('ModeBadge', () => {
  it('renders the mode label', () => {
    render(<ModeBadge mode="heartbeat" />)
    expect(screen.getByText('heartbeat')).toBeInTheDocument()
  })

  it('uses an aria-label describing the mode', () => {
    render(<ModeBadge mode="observe" />)
    expect(screen.getByLabelText('Integration mode observe')).toBeInTheDocument()
  })
})

describe('StateBadge', () => {
  it('renders ok state', () => {
    render(<StateBadge state="ok" />)
    expect(screen.getByText('ok')).toBeInTheDocument()
  })

  it('renders failed state with critical aria-label', () => {
    render(<StateBadge state="failed" />)
    expect(screen.getByLabelText('Last seen state failed')).toBeInTheDocument()
  })
})
