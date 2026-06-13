import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { MetricsSystemTab } from './MetricsSystemTab'

afterEach(() => {
  cleanup()
})

describe('MetricsSystemTab', () => {
  it('embeds the host-overview Grafana dashboard in kiosk mode', () => {
    render(<MetricsSystemTab />)
    const iframe = screen.getByTitle('System metrics (Grafana)')
    expect(iframe.getAttribute('src')).toBe('/api/grafana/d/host-overview/host-overview?kiosk')
  })
})
