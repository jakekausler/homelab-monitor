import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { HaIntegrationStatusWidget } from './HaIntegrationStatusWidget'

afterEach(() => {
  cleanup()
})

describe('HaIntegrationStatusWidget', () => {
  it('renders all four counts when there are actionable signals', () => {
    render(
      <HaIntegrationStatusWidget
        configEntries={{ loaded: 30, error: 2 }}
        repairs={1}
        notifications={3}
      />,
    )
    expect(screen.getByText('30')).toBeInTheDocument()
    expect(screen.getByText('2')).toBeInTheDocument()
    expect(screen.getByText('1')).toBeInTheDocument()
    expect(screen.getByText('3')).toBeInTheDocument()
  })

  it('tints Errors red when error > 0', () => {
    render(
      <HaIntegrationStatusWidget
        configEntries={{ loaded: 30, error: 2 }}
        repairs={0}
        notifications={0}
      />,
    )
    const errorValue = screen.getByText('2')
    expect(errorValue.className).toContain('text-red-700')
  })

  it('tints Repairs amber when repairs > 0', () => {
    render(
      <HaIntegrationStatusWidget
        configEntries={{ loaded: 30, error: 0 }}
        repairs={1}
        notifications={0}
      />,
    )
    const repairsValue = screen.getByText('1')
    expect(repairsValue.className).toContain('text-amber-700')
  })

  it('tints Notifications amber when notifications > 0', () => {
    render(
      <HaIntegrationStatusWidget
        configEntries={{ loaded: 30, error: 0 }}
        repairs={0}
        notifications={3}
      />,
    )
    const notifValue = screen.getByText('3')
    expect(notifValue.className).toContain('text-amber-700')
  })

  it('renders EmptyState when error=0, repairs=0, notifications=0 (even if loaded > 0)', () => {
    render(
      <HaIntegrationStatusWidget
        configEntries={{ loaded: 30, error: 0 }}
        repairs={0}
        notifications={0}
      />,
    )
    expect(screen.getByText('All integrations healthy')).toBeInTheDocument()
    expect(screen.queryByText('30')).not.toBeInTheDocument()
  })
})
