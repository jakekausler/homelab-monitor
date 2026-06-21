import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import React, { type ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('./client', () => ({
  apiClient: {
    GET: vi.fn(),
  },
  unwrap: vi.fn((result: { data?: unknown; error?: unknown; response: Response }) => {
    if (result.data !== undefined) return result.data
    throw new Error('mocked api error')
  }),
}))

import { apiClient } from './client'
import {
  useUnifiSummary,
  useUnifiDevices,
  useUnifiDevice,
  useUnifiThreats,
  useUnifiDpi,
  useUnifiTeleport,
  useUnifiControllerHealth,
  useUnifiWan,
  useUnifiDhcp,
  useUnifiWifi,
  useUnifiDnsPosture,
  useUnifiClients,
  useUnifiClient,
} from './unifi'

function fakeResponse(status: number): Response {
  return new Response(null, { status })
}

function makeWrapper(): ({ children }: { children: ReactNode }) => ReactNode {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  })
  return ({ children }: { children: ReactNode }) =>
    React.createElement(QueryClientProvider, { client }, children)
}

describe('unifi hooks', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  describe('useUnifiSummary', () => {
    it('calls GET /api/integrations/unifi/summary and returns unwrapped data', async () => {
      const mockData = { controller_up: true, devices_total: 3, devices_up: 3, clients_total: 10 }
      vi.mocked(apiClient.GET).mockResolvedValue({ data: mockData, response: fakeResponse(200) })

      const { result } = renderHook(() => useUnifiSummary(), { wrapper: makeWrapper() })

      await waitFor(() => expect(result.current.isSuccess).toBe(true))

      expect(apiClient.GET).toHaveBeenCalledWith('/api/integrations/unifi/summary', {})
      expect(result.current.data).toEqual(mockData)
    })
  })

  describe('useUnifiDevices', () => {
    it('calls GET /api/integrations/unifi/devices and returns device list', async () => {
      const mockData = { devices: [] }
      vi.mocked(apiClient.GET).mockResolvedValue({ data: mockData, response: fakeResponse(200) })

      const { result } = renderHook(() => useUnifiDevices(), { wrapper: makeWrapper() })

      await waitFor(() => expect(result.current.isSuccess).toBe(true))

      expect(apiClient.GET).toHaveBeenCalledWith('/api/integrations/unifi/devices', {})
      expect(result.current.data).toEqual(mockData)
    })
  })

  describe('useUnifiDevice', () => {
    it('calls GET /api/integrations/unifi/devices/{device} when mac is provided', async () => {
      const mockData = {
        mac: 'aa:bb:cc:dd:ee:ff',
        cpu_pct: 10,
        mem_pct: 20,
        load: 0.5,
        ports: [],
        radios: [],
        outlets: [],
        temps: [],
      }
      vi.mocked(apiClient.GET).mockResolvedValue({ data: mockData, response: fakeResponse(200) })

      const { result } = renderHook(() => useUnifiDevice('aa:bb:cc:dd:ee:ff'), {
        wrapper: makeWrapper(),
      })

      await waitFor(() => expect(result.current.isSuccess).toBe(true))

      expect(apiClient.GET).toHaveBeenCalledWith('/api/integrations/unifi/devices/{device}', {
        params: { path: { device: 'aa:bb:cc:dd:ee:ff' } },
      })
    })

    it('is disabled (queryFn not called) when device is empty string', () => {
      const { result } = renderHook(() => useUnifiDevice(''), { wrapper: makeWrapper() })

      // isPending stays true (enabled:false, query never fires)
      expect(result.current.isPending).toBe(true)
      expect(apiClient.GET).not.toHaveBeenCalled()
    })
  })

  describe('useUnifiThreats', () => {
    it('calls GET /api/integrations/unifi/threats and returns data', async () => {
      const mockData = { threats: [] }
      vi.mocked(apiClient.GET).mockResolvedValue({ data: mockData, response: fakeResponse(200) })

      const { result } = renderHook(() => useUnifiThreats(), { wrapper: makeWrapper() })

      await waitFor(() => expect(result.current.isSuccess).toBe(true))

      expect(apiClient.GET).toHaveBeenCalledWith('/api/integrations/unifi/threats', {})
    })
  })

  describe('useUnifiDpi', () => {
    it('calls GET /api/integrations/unifi/dpi and returns data', async () => {
      const mockData = { apps: [] }
      vi.mocked(apiClient.GET).mockResolvedValue({ data: mockData, response: fakeResponse(200) })

      const { result } = renderHook(() => useUnifiDpi(), { wrapper: makeWrapper() })

      await waitFor(() => expect(result.current.isSuccess).toBe(true))

      expect(apiClient.GET).toHaveBeenCalledWith('/api/integrations/unifi/dpi', {})
    })
  })

  describe('useUnifiTeleport', () => {
    it('calls GET /api/integrations/unifi/teleport and returns data', async () => {
      const mockData = { teleport_up: true, version: '1.0.0', reason: null }
      vi.mocked(apiClient.GET).mockResolvedValue({ data: mockData, response: fakeResponse(200) })

      const { result } = renderHook(() => useUnifiTeleport(), { wrapper: makeWrapper() })

      await waitFor(() => expect(result.current.isSuccess).toBe(true))

      expect(apiClient.GET).toHaveBeenCalledWith('/api/integrations/unifi/teleport', {})
    })
  })

  describe('useUnifiControllerHealth', () => {
    it('calls GET /api/integrations/unifi/controller-health and returns data', async () => {
      const mockData = { controller_up: true, up_reasons: [], api_took_seconds: [] }
      vi.mocked(apiClient.GET).mockResolvedValue({ data: mockData, response: fakeResponse(200) })

      const { result } = renderHook(() => useUnifiControllerHealth(), { wrapper: makeWrapper() })

      await waitFor(() => expect(result.current.isSuccess).toBe(true))

      expect(apiClient.GET).toHaveBeenCalledWith('/api/integrations/unifi/controller-health', {})
    })
  })

  describe('useUnifiWan', () => {
    it('calls GET /api/integrations/unifi/network/wan and returns data', async () => {
      const mockData = { wan_up: true }
      vi.mocked(apiClient.GET).mockResolvedValue({ data: mockData, response: fakeResponse(200) })
      const { result } = renderHook(() => useUnifiWan(), { wrapper: makeWrapper() })
      await waitFor(() => expect(result.current.isSuccess).toBe(true))
      expect(apiClient.GET).toHaveBeenCalledWith('/api/integrations/unifi/network/wan', {})
      expect(result.current.data).toEqual(mockData)
    })
  })

  describe('useUnifiDhcp', () => {
    it('calls GET /api/integrations/unifi/network/dhcp and returns data', async () => {
      const mockData = { networks: [] }
      vi.mocked(apiClient.GET).mockResolvedValue({ data: mockData, response: fakeResponse(200) })
      const { result } = renderHook(() => useUnifiDhcp(), { wrapper: makeWrapper() })
      await waitFor(() => expect(result.current.isSuccess).toBe(true))
      expect(apiClient.GET).toHaveBeenCalledWith('/api/integrations/unifi/network/dhcp', {})
    })
  })

  describe('useUnifiWifi', () => {
    it('calls GET /api/integrations/unifi/network/wifi and returns data', async () => {
      const mockData = { poor_signal: 0, by_band: [], by_link: [], ssids: [] }
      vi.mocked(apiClient.GET).mockResolvedValue({ data: mockData, response: fakeResponse(200) })
      const { result } = renderHook(() => useUnifiWifi(), { wrapper: makeWrapper() })
      await waitFor(() => expect(result.current.isSuccess).toBe(true))
      expect(apiClient.GET).toHaveBeenCalledWith('/api/integrations/unifi/network/wifi', {})
    })
  })

  describe('useUnifiDnsPosture', () => {
    it('calls GET /api/integrations/unifi/network/dns-posture and returns data', async () => {
      const mockData = { networks: [] }
      vi.mocked(apiClient.GET).mockResolvedValue({ data: mockData, response: fakeResponse(200) })
      const { result } = renderHook(() => useUnifiDnsPosture(), { wrapper: makeWrapper() })
      await waitFor(() => expect(result.current.isSuccess).toBe(true))
      expect(apiClient.GET).toHaveBeenCalledWith('/api/integrations/unifi/network/dns-posture', {})
    })
  })

  describe('useUnifiClients', () => {
    it('calls GET /api/integrations/unifi/clients with limit/offset and returns data', async () => {
      const mockData = { clients: [], limit: 500, offset: 0, total: 0 }
      vi.mocked(apiClient.GET).mockResolvedValue({ data: mockData, response: fakeResponse(200) })

      const { result } = renderHook(() => useUnifiClients(500, 0), { wrapper: makeWrapper() })

      await waitFor(() => expect(result.current.isSuccess).toBe(true))

      expect(apiClient.GET).toHaveBeenCalledWith('/api/integrations/unifi/clients', {
        params: { query: { limit: 500, offset: 0 } },
      })
      expect(result.current.data).toEqual(mockData)
    })
  })

  describe('useUnifiClient', () => {
    it('calls GET /api/integrations/unifi/clients/{mac} when mac is provided', async () => {
      const mockData = {
        mac: 'aa:bb:cc:dd:ee:ff',
        name: 'Box',
        hostname: null,
        ip: '192.168.1.5',
        network: 'LAN',
        is_host: false,
        online: true,
        ap_mac: null,
        sw_mac: null,
        sw_port: null,
        oui: null,
        first_seen: '2026-06-20T00:00:00Z',
        last_seen: '2026-06-20T00:00:00Z',
        lease_expiry: null,
        fixed_ip: null,
        use_fixedip: false,
        dns: null,
        dpi: [],
        series: { signal_dbm: null, tx_rate_bps: null, rx_rate_bps: null },
      }
      vi.mocked(apiClient.GET).mockResolvedValue({ data: mockData, response: fakeResponse(200) })

      const { result } = renderHook(() => useUnifiClient('aa:bb:cc:dd:ee:ff'), {
        wrapper: makeWrapper(),
      })

      await waitFor(() => expect(result.current.isSuccess).toBe(true))

      expect(apiClient.GET).toHaveBeenCalledWith('/api/integrations/unifi/clients/{mac}', {
        params: { path: { mac: 'aa:bb:cc:dd:ee:ff' } },
      })
    })

    it('is disabled (queryFn not called) when mac is empty string', () => {
      const { result } = renderHook(() => useUnifiClient(''), { wrapper: makeWrapper() })
      expect(result.current.isPending).toBe(true)
      expect(apiClient.GET).not.toHaveBeenCalled()
    })
  })
})
