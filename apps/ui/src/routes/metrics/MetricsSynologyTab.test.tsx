import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { MetricsSynologyTab } from './MetricsSynologyTab'

afterEach(() => {
  cleanup()
})

describe('MetricsSynologyTab', () => {
  it('embeds the synology Grafana dashboard in kiosk mode', () => {
    render(<MetricsSynologyTab />)
    const iframe = screen.getByTitle('Synology metrics (Grafana)')
    expect(iframe.getAttribute('src')).toBe('/api/grafana/d/synology/synology?kiosk')
  })
})
