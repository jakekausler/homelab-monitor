import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

import type { Schema } from '@/api/types'

import { NetworkDnsPostureWidget } from './NetworkDnsPostureWidget'

type UnifiDnsPostureResponse = Schema<'UnifiDnsPostureResponse'>

afterEach(() => {
  cleanup()
})

describe('NetworkDnsPostureWidget', () => {
  it('renders the honest empty state when no networks', () => {
    const data: UnifiDnsPostureResponse = { networks: [] }
    render(<NetworkDnsPostureWidget data={data} />)
    expect(screen.getByText(/No per-network DNS overrides configured/i)).toBeInTheDocument()
  })

  it('renders a red Drift badge when a network drifts', () => {
    const data: UnifiDnsPostureResponse = {
      networks: [
        { network: 'LAN', dns: '192.168.2.1', expected_dns: '192.168.2.148', drift: true },
      ],
    }
    render(<NetworkDnsPostureWidget data={data} />)
    expect(screen.getByText('LAN')).toBeInTheDocument()
    expect(screen.getByText('192.168.2.1')).toBeInTheDocument()
    expect(screen.getByText('Drift')).toBeInTheDocument()
    expect(screen.queryByText('OK')).not.toBeInTheDocument()
  })

  it('renders a green OK badge when DNS matches expected', () => {
    const data: UnifiDnsPostureResponse = {
      networks: [
        { network: 'LAN', dns: '192.168.2.148', expected_dns: '192.168.2.148', drift: false },
      ],
    }
    render(<NetworkDnsPostureWidget data={data} />)
    expect(screen.getByText('OK')).toBeInTheDocument()
    expect(screen.queryByText('Drift')).not.toBeInTheDocument()
  })

  it('renders no badge when expected_dns is null', () => {
    const data: UnifiDnsPostureResponse = {
      networks: [{ network: 'LAN', dns: '192.168.2.1', expected_dns: null, drift: false }],
    }
    render(<NetworkDnsPostureWidget data={data} />)
    expect(screen.getByText('LAN')).toBeInTheDocument()
    expect(screen.getByText('192.168.2.1')).toBeInTheDocument()
    expect(screen.queryByText('OK')).not.toBeInTheDocument()
    expect(screen.queryByText('Drift')).not.toBeInTheDocument()
  })
})
