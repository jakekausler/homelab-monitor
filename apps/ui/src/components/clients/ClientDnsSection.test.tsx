import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

import { ClientDnsSection } from './ClientDnsSection'

afterEach(() => cleanup())

describe('ClientDnsSection', () => {
  it('null prop renders honest placeholder with no fake numbers', () => {
    render(<ClientDnsSection dns={null} />)
    expect(screen.getByText(/provided by Pi-hole/i)).toBeInTheDocument()
    // assert NO fabricated zero-count leaks into the placeholder
    expect(screen.queryByText('0')).not.toBeInTheDocument()
    expect(screen.queryByTestId('dns-top-domains')).not.toBeInTheDocument()
  })

  it('populated prop renders blocked_count, last_query_at, and top_domains', () => {
    render(
      <ClientDnsSection
        dns={{
          blocked_count: 12,
          last_query_at: '2026-06-20T00:00:00Z',
          top_domains: ['ads.example.com', 'tracker.test'],
        }}
      />,
    )
    expect(screen.getByText('12')).toBeInTheDocument()
    expect(screen.getByText('ads.example.com')).toBeInTheDocument()
    expect(screen.getByText('tracker.test')).toBeInTheDocument()
  })

  it('populated prop with null counts renders em-dashes', () => {
    render(<ClientDnsSection dns={{ blocked_count: null, last_query_at: null, top_domains: [] }} />)
    // both blocked_count and last_query_at render "—"; assert at least one and the empty-domains copy
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText('No top domains.')).toBeInTheDocument()
  })
})
