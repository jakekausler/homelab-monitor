import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { MetricsStorageLogsTab } from './MetricsStorageLogsTab'

afterEach(() => {
  cleanup()
})

describe('MetricsStorageLogsTab', () => {
  it('embeds the storage-logs Grafana dashboard in kiosk mode', () => {
    render(<MetricsStorageLogsTab />)
    const iframe = screen.getByTitle('Storage & Logs metrics (Grafana)')
    expect(iframe.getAttribute('src')).toBe('/api/grafana/d/storage-logs/storage-logs?kiosk')
  })
})
