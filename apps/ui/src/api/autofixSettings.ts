import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query'

import { apiClient, type ApiError, unwrap } from './client'
import type { Schema } from './types'

export type KillSwitchState = Schema<'KillSwitchState'>
export type KillSwitchToggleRequest = Schema<'KillSwitchToggleRequest'>
export type KillSwitchToggleResponse = Schema<'KillSwitchToggleResponse'>

export const autofixSettingsKeys = {
  all: ['autofix-settings'] as const,
  killSwitch: () => ['autofix-settings', 'kill-switch'] as const,
}

export function useAutofixKillSwitch(): UseQueryResult<KillSwitchState, ApiError> {
  return useQuery({
    queryKey: autofixSettingsKeys.killSwitch(),
    queryFn: async () => {
      const result = await apiClient.GET('/api/settings/autofix/kill-switch', {})
      return unwrap<KillSwitchState>(result)
    },
    retry: false,
  })
}

export function useToggleAutofixKillSwitch(): UseMutationResult<
  KillSwitchToggleResponse,
  ApiError,
  KillSwitchToggleRequest
> {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: KillSwitchToggleRequest) => {
      const result = await apiClient.POST('/api/settings/autofix/kill-switch', { body })
      return unwrap<KillSwitchToggleResponse>(result)
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: autofixSettingsKeys.all })
    },
  })
}
