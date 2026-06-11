import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render } from '@testing-library/react'

import { ActiveAlertsTab } from '../ActiveAlertsTab'

afterEach(() => {
  cleanup()
})

describe('ActiveAlertsTab', () => {
  it('renders the Karma iframe pointed at /api/karma/', () => {
    const { container } = render(<ActiveAlertsTab />)
    const iframe = container.querySelector('iframe')
    expect(iframe).not.toBeNull()
    expect(iframe!.getAttribute('src')).toBe('/api/karma/')
  })

  it('locks the iframe sandbox attribute to the Design-approved package (security-relevant)', () => {
    const { container } = render(<ActiveAlertsTab />)
    const iframe = container.querySelector('iframe')
    expect(iframe!.getAttribute('sandbox')).toBe('allow-scripts allow-same-origin allow-forms')
  })

  it('gives the iframe an accessible title', () => {
    const { container } = render(<ActiveAlertsTab />)
    const iframe = container.querySelector('iframe')
    expect(iframe!.getAttribute('title')).toBe('Alerts (Karma)')
  })
})
