import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { MetricsPiholeTab } from './MetricsPiholeTab'

afterEach(() => {
  cleanup()
})

describe('MetricsPiholeTab', () => {
  it('embeds the pihole Grafana dashboard in kiosk mode', () => {
    render(<MetricsPiholeTab />)
    const iframe = screen.getByTitle('Pi-hole metrics (Grafana)')
    expect(iframe.getAttribute('src')).toBe('/api/grafana/d/pihole/pihole?kiosk')
  })
})
