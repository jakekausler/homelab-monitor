import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query'

import { apiClient, type ApiError, unwrap } from './client'
import type { Schema } from './types'

export type PinStatusResponse = Schema<'PinStatusResponse'>
export type SetPinResponse = Schema<'SetPinResponse'>
export type VerifyPinResponse = Schema<'VerifyPinResponse'>
export type DeletePinResponse = Schema<'DeletePinResponse'>

export const pinKeys = {
  all: ['security-pin'] as const,
  status: () => [...pinKeys.all, 'status'] as const,
}

/** GET /api/settings/security/pin - check PIN status */
export function usePinStatus(): UseQueryResult<PinStatusResponse, ApiError> {
  return useQuery({
    queryKey: pinKeys.status(),
    queryFn: async () => {
      const result = await apiClient.GET('/api/settings/security/pin', {})
      return unwrap<PinStatusResponse>(result)
    },
    staleTime: 30_000,
    retry: false,
  })
}

/** POST /api/settings/security/pin - set or rotate PIN */
export function useSetPin(): UseMutationResult<
  SetPinResponse,
  ApiError,
  { new_pin: string; current_password?: string; current_pin?: string }
> {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: {
      new_pin: string
      current_password?: string
      current_pin?: string
    }) => {
      const result = await apiClient.POST('/api/settings/security/pin', { body })
      return unwrap<SetPinResponse>(result)
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: pinKeys.status() })
    },
  })
}

/** POST /api/settings/security/pin/verify - verify PIN (test verification, no side effects) */
export function useVerifyPin(): UseMutationResult<VerifyPinResponse, ApiError, { pin: string }> {
  return useMutation({
    mutationFn: async (body: { pin: string }) => {
      const result = await apiClient.POST('/api/settings/security/pin/verify', { body })
      return unwrap<VerifyPinResponse>(result)
    },
  })
}

/** DELETE /api/settings/security/pin - delete/remove PIN */
export function useDeletePin(): UseMutationResult<
  DeletePinResponse,
  ApiError,
  { current_password: string }
> {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: { current_password: string }) => {
      const result = await apiClient.DELETE('/api/settings/security/pin', { body })
      return unwrap<DeletePinResponse>(result)
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: pinKeys.status() })
    },
  })
}
