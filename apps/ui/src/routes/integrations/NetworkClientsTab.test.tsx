import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

import { NetworkClientsTab } from './NetworkClientsTab'

afterEach(() => cleanup())

describe('NetworkClientsTab', () => {
  it('renders the honest coming-soon placeholder', () => {
    render(<NetworkClientsTab />)
    const status = screen.getByRole('status')
    expect(status).toHaveTextContent('Client inventory arrives in STAGE-007-022.')
  })
})
