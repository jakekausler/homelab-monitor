import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { MetricsNetworkTab } from './MetricsNetworkTab'

afterEach(() => {
  cleanup()
})

describe('MetricsNetworkTab', () => {
  it('embeds the network Grafana dashboard in kiosk mode', () => {
    render(<MetricsNetworkTab />)
    const iframe = screen.getByTitle('Network metrics (Grafana)')
    expect(iframe.getAttribute('src')).toBe('/api/grafana/d/network/network?kiosk')
  })
})
