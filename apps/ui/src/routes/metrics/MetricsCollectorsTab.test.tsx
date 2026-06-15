import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { MetricsCollectorsTab } from './MetricsCollectorsTab'

afterEach(() => {
  cleanup()
})

describe('MetricsCollectorsTab', () => {
  it('embeds the collectors Grafana dashboard in kiosk mode', () => {
    render(<MetricsCollectorsTab />)
    const iframe = screen.getByTitle('Collectors metrics (Grafana)')
    expect(iframe.getAttribute('src')).toBe('/api/grafana/d/collectors/collectors?kiosk')
  })
})
