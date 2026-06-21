import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { MetricsUnifiTab } from './MetricsUnifiTab'

afterEach(() => {
  cleanup()
})

describe('MetricsUnifiTab', () => {
  it('embeds the homelab-unifi Grafana dashboard in kiosk mode', () => {
    render(<MetricsUnifiTab />)
    const iframe = screen.getByTitle('Unifi metrics (Grafana)')
    expect(iframe.getAttribute('src')).toBe('/api/grafana/d/homelab-unifi/homelab-unifi?kiosk')
  })
})
