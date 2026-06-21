import { useQuery, type UseQueryResult } from '@tanstack/react-query'

import { apiClient, ApiError, unwrap } from './client'
import type { Schema } from './types'

type UnifiSummary = Schema<'UnifiSummary'>
type UnifiDevicesResponse = Schema<'UnifiDevicesResponse'>
type UnifiDeviceDetail = Schema<'UnifiDeviceDetail'>
type UnifiThreatsResponse = Schema<'UnifiThreatsResponse'>
type UnifiDpiResponse = Schema<'UnifiDpiResponse'>
type UnifiTeleport = Schema<'UnifiTeleport'>
type UnifiControllerHealth = Schema<'UnifiControllerHealth'>
type UnifiWanCurrent = Schema<'UnifiWanCurrent'>
type UnifiNetworkDhcpResponse = Schema<'UnifiNetworkDhcpResponse'>
type UnifiWifiResponse = Schema<'UnifiWifiResponse'>
type UnifiDnsPostureResponse = Schema<'UnifiDnsPostureResponse'>

export const unifiQueryKeys = {
  summary: ['integrations', 'unifi', 'summary'] as const,
  devices: ['integrations', 'unifi', 'devices'] as const,
  device: (device: string) => ['integrations', 'unifi', 'device', device] as const,
  threats: ['integrations', 'unifi', 'threats'] as const,
  dpi: ['integrations', 'unifi', 'dpi'] as const,
  teleport: ['integrations', 'unifi', 'teleport'] as const,
  controllerHealth: ['integrations', 'unifi', 'controller-health'] as const,
  networkWan: ['integrations', 'unifi', 'network', 'wan'] as const,
  networkDhcp: ['integrations', 'unifi', 'network', 'dhcp'] as const,
  networkWifi: ['integrations', 'unifi', 'network', 'wifi'] as const,
  networkDnsPosture: ['integrations', 'unifi', 'network', 'dns-posture'] as const,
}

const REFETCH_INTERVAL_MS = 30_000

export function useUnifiSummary(): UseQueryResult<UnifiSummary, ApiError> {
  return useQuery({
    queryKey: unifiQueryKeys.summary,
    queryFn: async () => {
      const result = await apiClient.GET('/api/integrations/unifi/summary', {})
      return unwrap<UnifiSummary>(result)
    },
    refetchInterval: REFETCH_INTERVAL_MS,
  })
}

export function useUnifiDevices(): UseQueryResult<UnifiDevicesResponse, ApiError> {
  return useQuery({
    queryKey: unifiQueryKeys.devices,
    queryFn: async () => {
      const result = await apiClient.GET('/api/integrations/unifi/devices', {})
      return unwrap<UnifiDevicesResponse>(result)
    },
    refetchInterval: REFETCH_INTERVAL_MS,
  })
}

export function useUnifiDevice(device: string): UseQueryResult<UnifiDeviceDetail, ApiError> {
  return useQuery({
    queryKey: unifiQueryKeys.device(device),
    queryFn: async () => {
      const result = await apiClient.GET('/api/integrations/unifi/devices/{device}', {
        params: { path: { device } },
      })
      return unwrap<UnifiDeviceDetail>(result)
    },
    enabled: device.length > 0,
    refetchInterval: REFETCH_INTERVAL_MS,
  })
}

export function useUnifiThreats(): UseQueryResult<UnifiThreatsResponse, ApiError> {
  return useQuery({
    queryKey: unifiQueryKeys.threats,
    queryFn: async () => {
      const result = await apiClient.GET('/api/integrations/unifi/threats', {})
      return unwrap<UnifiThreatsResponse>(result)
    },
    refetchInterval: REFETCH_INTERVAL_MS,
  })
}

export function useUnifiDpi(): UseQueryResult<UnifiDpiResponse, ApiError> {
  return useQuery({
    queryKey: unifiQueryKeys.dpi,
    queryFn: async () => {
      const result = await apiClient.GET('/api/integrations/unifi/dpi', {})
      return unwrap<UnifiDpiResponse>(result)
    },
    refetchInterval: REFETCH_INTERVAL_MS,
  })
}

export function useUnifiTeleport(): UseQueryResult<UnifiTeleport, ApiError> {
  return useQuery({
    queryKey: unifiQueryKeys.teleport,
    queryFn: async () => {
      const result = await apiClient.GET('/api/integrations/unifi/teleport', {})
      return unwrap<UnifiTeleport>(result)
    },
    refetchInterval: REFETCH_INTERVAL_MS,
  })
}

export function useUnifiControllerHealth(): UseQueryResult<UnifiControllerHealth, ApiError> {
  return useQuery({
    queryKey: unifiQueryKeys.controllerHealth,
    queryFn: async () => {
      const result = await apiClient.GET('/api/integrations/unifi/controller-health', {})
      return unwrap<UnifiControllerHealth>(result)
    },
    refetchInterval: REFETCH_INTERVAL_MS,
  })
}

export function useUnifiWan(): UseQueryResult<UnifiWanCurrent, ApiError> {
  return useQuery({
    queryKey: unifiQueryKeys.networkWan,
    queryFn: async () => {
      const result = await apiClient.GET('/api/integrations/unifi/network/wan', {})
      return unwrap<UnifiWanCurrent>(result)
    },
    refetchInterval: REFETCH_INTERVAL_MS,
  })
}

export function useUnifiDhcp(): UseQueryResult<UnifiNetworkDhcpResponse, ApiError> {
  return useQuery({
    queryKey: unifiQueryKeys.networkDhcp,
    queryFn: async () => {
      const result = await apiClient.GET('/api/integrations/unifi/network/dhcp', {})
      return unwrap<UnifiNetworkDhcpResponse>(result)
    },
    refetchInterval: REFETCH_INTERVAL_MS,
  })
}

export function useUnifiWifi(): UseQueryResult<UnifiWifiResponse, ApiError> {
  return useQuery({
    queryKey: unifiQueryKeys.networkWifi,
    queryFn: async () => {
      const result = await apiClient.GET('/api/integrations/unifi/network/wifi', {})
      return unwrap<UnifiWifiResponse>(result)
    },
    refetchInterval: REFETCH_INTERVAL_MS,
  })
}

export function useUnifiDnsPosture(): UseQueryResult<UnifiDnsPostureResponse, ApiError> {
  return useQuery({
    queryKey: unifiQueryKeys.networkDnsPosture,
    queryFn: async () => {
      const result = await apiClient.GET('/api/integrations/unifi/network/dns-posture', {})
      return unwrap<UnifiDnsPostureResponse>(result)
    },
    refetchInterval: REFETCH_INTERVAL_MS,
  })
}
