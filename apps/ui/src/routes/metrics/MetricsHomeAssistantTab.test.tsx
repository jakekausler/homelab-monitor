import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { MetricsHomeAssistantTab } from './MetricsHomeAssistantTab'

afterEach(() => {
  cleanup()
})

describe('MetricsHomeAssistantTab', () => {
  it('embeds the home-assistant Grafana dashboard in kiosk mode', () => {
    render(<MetricsHomeAssistantTab />)
    const iframe = screen.getByTitle('Home Assistant metrics (Grafana)')
    expect(iframe.getAttribute('src')).toBe('/api/grafana/d/home-assistant/home-assistant?kiosk')
  })
})
