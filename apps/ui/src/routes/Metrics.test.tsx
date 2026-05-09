import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { MetricsPage } from './Metrics'

afterEach(() => {
  cleanup()
})

describe('MetricsPage', () => {
  it('renders the Grafana iframe with the correct kiosk-mode src', () => {
    render(<MetricsPage />)
    const iframe = screen.getByTitle('Metrics (Grafana)')
    expect(iframe).toBeInTheDocument()
    expect(iframe.getAttribute('src')).toBe('/api/grafana/d/host-overview/host-overview?kiosk')
  })

  it('applies the locked sandbox attribute', () => {
    render(<MetricsPage />)
    const iframe = screen.getByTitle('Metrics (Grafana)')
    expect(iframe.getAttribute('sandbox')).toBe('allow-scripts allow-same-origin allow-forms')
  })

  it('uses full-bleed layout classes', () => {
    const { container } = render(<MetricsPage />)
    const wrapper = container.firstElementChild as HTMLElement
    expect(wrapper).not.toBeNull()
    expect(wrapper.className).toContain('-m-6')
    expect(wrapper.className).toContain('h-[calc(100vh-3.5rem)]')
  })

  it('iframe has zero border and full size classes', () => {
    render(<MetricsPage />)
    const iframe = screen.getByTitle('Metrics (Grafana)')
    expect(iframe.className).toContain('border-0')
    expect(iframe.className).toContain('h-full')
    expect(iframe.className).toContain('w-full')
  })

  it('renders the "Open in Grafana" link with target=_blank', () => {
    render(<MetricsPage />)
    const link = screen.getByRole('link', { name: /Open in Grafana/ })
    expect(link).toBeInTheDocument()
    expect(link.getAttribute('href')).toBe('/api/grafana/')
    expect(link.getAttribute('target')).toBe('_blank')
    expect(link.getAttribute('rel')).toBe('noopener noreferrer')
  })
})
