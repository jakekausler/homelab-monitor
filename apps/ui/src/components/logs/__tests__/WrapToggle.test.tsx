import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { WrapToggle } from '@/components/logs/WrapToggle'

afterEach(cleanup)

describe('WrapToggle', () => {
  it('renders the toggle with label', () => {
    render(<WrapToggle checked={false} onChange={() => {}} />)
    expect(screen.getByTestId('wrap-toggle')).toBeInTheDocument()
    expect(screen.getByTestId('wrap-toggle')).toHaveTextContent('Wrap')
  })

  it('reflects checked=false', () => {
    render(<WrapToggle checked={false} onChange={() => {}} />)
    const input = screen.getByRole('checkbox')
    expect(input).not.toBeChecked()
  })

  it('reflects checked=true', () => {
    render(<WrapToggle checked={true} onChange={() => {}} />)
    const input = screen.getByRole('checkbox')
    expect(input).toBeChecked()
  })

  it('calls onChange with the toggled value', () => {
    const onChange = vi.fn()
    render(<WrapToggle checked={false} onChange={onChange} />)
    fireEvent.click(screen.getByRole('checkbox'))
    expect(onChange).toHaveBeenCalledWith(true)
  })

  it('calls onChange with false when unchecking', () => {
    const onChange = vi.fn()
    render(<WrapToggle checked={true} onChange={onChange} />)
    fireEvent.click(screen.getByRole('checkbox'))
    expect(onChange).toHaveBeenCalledWith(false)
  })
})
