import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { MetricsContainersTab } from './MetricsContainersTab'

afterEach(() => {
  cleanup()
})

describe('MetricsContainersTab', () => {
  it('embeds the containers Grafana dashboard in kiosk mode', () => {
    render(<MetricsContainersTab />)
    const iframe = screen.getByTitle('Containers metrics (Grafana)')
    expect(iframe.getAttribute('src')).toBe('/api/grafana/d/containers/containers?kiosk')
  })
})
