import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { AlertsPage } from './Alerts'

afterEach(() => {
  cleanup()
})

describe('AlertsPage', () => {
  it('renders the Karma iframe with the correct src', () => {
    render(<AlertsPage />)
    const iframe = screen.getByTitle('Alerts (Karma)')
    expect(iframe).toBeInTheDocument()
    expect(iframe.getAttribute('src')).toBe('/api/karma/')
  })

  it('applies the locked sandbox attribute', () => {
    render(<AlertsPage />)
    const iframe = screen.getByTitle('Alerts (Karma)')
    expect(iframe.getAttribute('sandbox')).toBe('allow-scripts allow-same-origin allow-forms')
  })

  it('uses full-bleed layout classes', () => {
    const { container } = render(<AlertsPage />)
    // Outer wrapper negates AppShell's p-6 and fills viewport height
    const wrapper = container.firstElementChild as HTMLElement
    expect(wrapper).not.toBeNull()
    expect(wrapper.className).toContain('-m-6')
    expect(wrapper.className).toContain('h-[calc(100vh-3.5rem)]')
  })

  it('iframe has zero border and full size classes', () => {
    render(<AlertsPage />)
    const iframe = screen.getByTitle('Alerts (Karma)')
    expect(iframe.className).toContain('border-0')
    expect(iframe.className).toContain('h-full')
    expect(iframe.className).toContain('w-full')
  })
})
