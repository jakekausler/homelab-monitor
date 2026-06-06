import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query'

import { apiClient, type ApiError, unwrap } from './client'
import type { Schema } from './types'

export type SignatureListResponse = Schema<'SignatureListResponse'>
export type SignatureResponse = Schema<'SignatureResponse'>
export type SignaturePatchRequest = Schema<'SignaturePatchRequest'>
export type SignatureSamplesResponse = Schema<'SignatureSamplesResponse'>

export interface SignatureFilter {
  service?: string | undefined
  status?: 'active' | 'suppressed' | 'expected' | undefined
  label_q?: string | undefined
  limit?: number | undefined
  offset?: number | undefined
}

export const signatureKeys = {
  all: ['signatures'] as const,
  list: (f: SignatureFilter) => ['signatures', 'list', f] as const,
  one: (h: string, s: string) => ['signatures', 'one', h, s] as const,
  samples: (h: string, s: string) => ['signatures', 'samples', h, s] as const,
}

export function useSignaturesQuery(
  filter: SignatureFilter,
): UseQueryResult<SignatureListResponse, ApiError> {
  return useQuery({
    queryKey: signatureKeys.list(filter),
    queryFn: async () => {
      const result = await apiClient.GET('/api/logs/signatures', {
        params: {
          query: {
            ...(filter.service !== undefined ? { service: filter.service } : {}),
            ...(filter.status !== undefined ? { status: filter.status } : {}),
            ...(filter.label_q !== undefined ? { label_q: filter.label_q } : {}),
            ...(filter.limit !== undefined ? { limit: filter.limit } : {}),
            ...(filter.offset !== undefined ? { offset: filter.offset } : {}),
          },
        },
      })
      return unwrap<SignatureListResponse>(result)
    },
    staleTime: 30_000,
    retry: false,
  })
}

export function useSignature(
  templateHash: string,
  serviceKey: string,
  enabled = true,
): UseQueryResult<SignatureResponse, ApiError> {
  return useQuery({
    queryKey: signatureKeys.one(templateHash, serviceKey),
    queryFn: async () => {
      const result = await apiClient.GET('/api/logs/signatures/{template_hash}/{service_key}', {
        params: { path: { template_hash: templateHash, service_key: serviceKey } },
      })
      return unwrap<SignatureResponse>(result)
    },
    enabled: enabled && templateHash.length > 0 && serviceKey.length > 0,
    staleTime: 30_000,
    retry: false,
  })
}

export function useSignatureSamples(
  templateHash: string,
  serviceKey: string,
  enabled = true,
): UseQueryResult<SignatureSamplesResponse, ApiError> {
  return useQuery({
    queryKey: signatureKeys.samples(templateHash, serviceKey),
    queryFn: async () => {
      const result = await apiClient.GET(
        '/api/logs/signatures/{template_hash}/{service_key}/samples',
        { params: { path: { template_hash: templateHash, service_key: serviceKey } } },
      )
      return unwrap<SignatureSamplesResponse>(result)
    },
    enabled: enabled && templateHash.length > 0 && serviceKey.length > 0,
    staleTime: 30_000,
    retry: false,
  })
}

export function useUpdateSignature(): UseMutationResult<
  SignatureResponse,
  ApiError,
  { templateHash: string; serviceKey: string; body: SignaturePatchRequest }
> {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ templateHash, serviceKey, body }) => {
      const result = await apiClient.PATCH('/api/logs/signatures/{template_hash}/{service_key}', {
        params: { path: { template_hash: templateHash, service_key: serviceKey } },
        body,
      })
      return unwrap<SignatureResponse>(result)
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: signatureKeys.all })
    },
  })
}
