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

  it('populated prop renders new rich DNS fields', () => {
    render(
      <ClientDnsSection
        dns={{
          blocked_count: 12,
          last_query_at: '2026-06-20T00:00:00Z',
          top_domains: ['ads.example.com', 'tracker.test'],
          query_volume: 100,
          block_rate: 0.12,
          top_blocked: ['ads.example.com'],
          top_permitted: ['cdn.example.com'],
          recent_blocks: [{ domain: 'ads.example.com', at: '2026-06-20T00:00:00Z' }],
          servfail_count: 3,
          dnssec_bogus_count: 2,
        }}
      />,
    )
    expect(screen.getByText('100')).toBeInTheDocument() // query_volume
    expect(screen.getByText('12.0%')).toBeInTheDocument() // block_rate
    expect(screen.getByTestId('dns-top-blocked')).toHaveTextContent('ads.example.com')
    expect(screen.getByTestId('dns-top-allowed')).toHaveTextContent('cdn.example.com')
    expect(screen.getByTestId('dns-recent-blocks')).toHaveTextContent('ads.example.com')
    expect(screen.getByText('3 SERVFAIL')).toBeInTheDocument()
    expect(screen.getByText('2 DNSSEC bogus')).toBeInTheDocument()
  })

  it('badge-absence test: no badges when counts are zero', () => {
    render(
      <ClientDnsSection
        dns={{
          blocked_count: 5,
          last_query_at: '2026-06-20T00:00:00Z',
          top_domains: [],
          query_volume: 50,
          block_rate: 0.1,
          top_blocked: [],
          top_permitted: [],
          recent_blocks: [],
          servfail_count: 0,
          dnssec_bogus_count: 0,
        }}
      />,
    )
    expect(screen.queryByTestId('dns-health-badges')).not.toBeInTheDocument()
    expect(screen.queryByText(/SERVFAIL/)).not.toBeInTheDocument()
    expect(screen.queryByText(/DNSSEC/)).not.toBeInTheDocument()
  })

  it('populated prop with null counts renders em-dashes', () => {
    render(
      <ClientDnsSection
        dns={{
          blocked_count: null,
          last_query_at: null,
          top_domains: [],
          query_volume: null,
          block_rate: null,
          top_blocked: [],
          top_permitted: [],
          recent_blocks: [],
          servfail_count: 0,
          dnssec_bogus_count: 0,
        }}
      />,
    )
    // multiple em-dashes for blocked_count, query_volume, block_rate, last_query_at
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(2)
    expect(screen.getByText('No top domains.')).toBeInTheDocument()
  })
})
