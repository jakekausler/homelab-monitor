import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { MetricsHeartbeatsTab } from './MetricsHeartbeatsTab'

afterEach(() => {
  cleanup()
})

describe('MetricsHeartbeatsTab', () => {
  it('embeds the heartbeats Grafana dashboard in kiosk mode', () => {
    render(<MetricsHeartbeatsTab />)
    const iframe = screen.getByTitle('Heartbeats metrics (Grafana)')
    expect(iframe.getAttribute('src')).toBe('/api/grafana/d/heartbeats/heartbeats?kiosk')
  })
})
