import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { TimezoneToggle } from '@/components/logs/TimezoneToggle'

afterEach(cleanup)

describe('TimezoneToggle', () => {
  it('renders the toggle with label', () => {
    render(<TimezoneToggle checked={false} onChange={() => {}} />)
    expect(screen.getByTestId('timezone-toggle')).toBeInTheDocument()
    expect(screen.getByTestId('timezone-toggle')).toHaveTextContent('UTC')
  })

  it('reflects checked=false (local)', () => {
    render(<TimezoneToggle checked={false} onChange={() => {}} />)
    expect(screen.getByRole('checkbox')).not.toBeChecked()
  })

  it('reflects checked=true (utc)', () => {
    render(<TimezoneToggle checked={true} onChange={() => {}} />)
    expect(screen.getByRole('checkbox')).toBeChecked()
  })

  it('calls onChange when clicked', () => {
    const onChange = vi.fn()
    render(<TimezoneToggle checked={false} onChange={onChange} />)
    fireEvent.click(screen.getByRole('checkbox'))
    expect(onChange).toHaveBeenCalledTimes(1)
  })

  it('associates the label with the input via id', () => {
    render(<TimezoneToggle checked={false} onChange={() => {}} id="tz-x" />)
    const input = screen.getByRole('checkbox')
    expect(input).toHaveAttribute('id', 'tz-x')
  })
})
