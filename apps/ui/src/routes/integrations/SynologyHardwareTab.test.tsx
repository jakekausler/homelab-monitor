// SCAFFOLD TEST: STAGE-008-025 — asserts the Hardware placeholder cites STAGE-008-026.
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

import { SynologyHardwareTab } from './SynologyHardwareTab'

afterEach(() => {
  cleanup()
})

describe('SynologyHardwareTab', () => {
  it('renders the hardware placeholder citing the future stage', () => {
    render(<SynologyHardwareTab />)
    const empty = screen.getByTestId('synology-hardware-empty')
    expect(empty).toBeInTheDocument()
    expect(empty).toHaveTextContent(/STAGE-008-026/)
  })
})
