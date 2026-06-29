import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { MetricsSurveillanceTab } from './MetricsSurveillanceTab'

afterEach(() => {
  cleanup()
})

describe('MetricsSurveillanceTab', () => {
  it('embeds the surveillance Grafana dashboard in kiosk mode', () => {
    render(<MetricsSurveillanceTab />)
    const iframe = screen.getByTitle('Surveillance metrics (Grafana)')
    expect(iframe.getAttribute('src')).toBe(
      '/api/grafana/d/synology-surveillance/synology-surveillance?kiosk',
    )
  })
})
