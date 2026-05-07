import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { Sparkline } from './Sparkline'

describe('Sparkline', () => {
  it('renders nothing when given an empty series', () => {
    const { container } = render(<Sparkline values={[]} ariaLabel="empty" />)
    expect(container.firstChild).toBeNull()
  })

  it('renders an svg with role img when values are present', () => {
    render(<Sparkline values={[1, 2, 3]} ariaLabel="cpu history" />)
    const svg = screen.getByRole('img', { name: 'cpu history' })
    expect(svg.tagName.toLowerCase()).toBe('svg')
  })

  it('renders a flat path when all values are equal', () => {
    const { container } = render(<Sparkline values={[5, 5, 5]} ariaLabel="flat" />)
    const path = container.querySelector('path')
    expect(path).not.toBeNull()
    expect(path?.getAttribute('d')).toContain('M0')
  })
})
