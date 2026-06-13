import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { GrafanaDashboardEmbed } from './GrafanaDashboardEmbed'

afterEach(() => {
  cleanup()
})

describe('GrafanaDashboardEmbed', () => {
  it('renders the iframe with the provided src and title', () => {
    render(<GrafanaDashboardEmbed src="/api/grafana/d/foo/foo?kiosk" title="Foo (Grafana)" />)
    const iframe = screen.getByTitle('Foo (Grafana)')
    expect(iframe).toBeInTheDocument()
    expect(iframe.getAttribute('src')).toBe('/api/grafana/d/foo/foo?kiosk')
  })

  it('applies the locked sandbox attribute', () => {
    render(<GrafanaDashboardEmbed src="/api/grafana/d/foo/foo?kiosk" title="Foo (Grafana)" />)
    const iframe = screen.getByTitle('Foo (Grafana)')
    expect(iframe.getAttribute('sandbox')).toBe('allow-scripts allow-same-origin allow-forms')
  })

  it('uses flex-fill layout classes', () => {
    const { container } = render(
      <GrafanaDashboardEmbed src="/api/grafana/d/foo/foo?kiosk" title="Foo (Grafana)" />,
    )
    const wrapper = container.firstElementChild as HTMLElement
    expect(wrapper).not.toBeNull()
    expect(wrapper.className).toContain('h-full')
    expect(wrapper.className).toContain('w-full')
    expect(wrapper.className).not.toContain('-m-6')
    expect(wrapper.className).not.toContain('h-[calc(100vh-3.5rem)]')
  })

  it('iframe has zero border and full size classes', () => {
    render(<GrafanaDashboardEmbed src="/api/grafana/d/foo/foo?kiosk" title="Foo (Grafana)" />)
    const iframe = screen.getByTitle('Foo (Grafana)')
    expect(iframe.className).toContain('border-0')
    expect(iframe.className).toContain('h-full')
    expect(iframe.className).toContain('w-full')
  })

  it('renders the "Open in Grafana" link with target=_blank', () => {
    render(<GrafanaDashboardEmbed src="/api/grafana/d/foo/foo?kiosk" title="Foo (Grafana)" />)
    const link = screen.getByRole('link', { name: /Open in Grafana/ })
    expect(link).toBeInTheDocument()
    expect(link.getAttribute('href')).toBe('/api/grafana/')
    expect(link.getAttribute('target')).toBe('_blank')
    expect(link.getAttribute('rel')).toBe('noopener noreferrer')
  })
})
