import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'

import { StateBadge } from '@/components/crons/badges'

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
