// SCAFFOLD TEST: STAGE-008-025 — asserts the Ops placeholder cites STAGE-008-027.
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

import { SynologyOpsTab } from './SynologyOpsTab'

afterEach(() => {
  cleanup()
})

describe('SynologyOpsTab', () => {
  it('renders the ops placeholder citing the future stage', () => {
    render(<SynologyOpsTab />)
    const empty = screen.getByTestId('synology-ops-empty')
    expect(empty).toBeInTheDocument()
    expect(empty).toHaveTextContent(/STAGE-008-027/)
  })
})
