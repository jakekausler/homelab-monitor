import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query'

import { apiClient, type ApiError, unwrap } from './client'
import type { Schema } from './types'

export type AnnotationListResponse = Schema<'AnnotationListResponse'>
export type AnnotationResponse = Schema<'AnnotationResponse'>
export type AnnotationCreateRequest = Schema<'AnnotationCreateRequest'>

export const annotationKeys = {
  all: ['annotations'] as const,
  list: (h: string, s: string) => ['annotations', 'list', h, s] as const,
}

export function useSignatureAnnotations(
  templateHash: string,
  serviceKey: string,
  enabled = true,
): UseQueryResult<AnnotationListResponse, ApiError> {
  return useQuery({
    queryKey: annotationKeys.list(templateHash, serviceKey),
    queryFn: async () => {
      const result = await apiClient.GET(
        '/api/logs/signatures/{template_hash}/{service_key}/annotations',
        { params: { path: { template_hash: templateHash, service_key: serviceKey } } },
      )
      return unwrap<AnnotationListResponse>(result)
    },
    enabled: enabled && templateHash.length > 0 && serviceKey.length > 0,
    staleTime: 30_000,
    retry: false,
  })
}

export function useAddAnnotation(): UseMutationResult<
  AnnotationResponse,
  ApiError,
  { templateHash: string; serviceKey: string; body: AnnotationCreateRequest }
> {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ templateHash, serviceKey, body }) => {
      const result = await apiClient.POST(
        '/api/logs/signatures/{template_hash}/{service_key}/annotations',
        {
          params: { path: { template_hash: templateHash, service_key: serviceKey } },
          body,
        },
      )
      return unwrap<AnnotationResponse>(result)
    },
    onSuccess: (_data, variables) => {
      void qc.invalidateQueries({
        queryKey: annotationKeys.list(variables.templateHash, variables.serviceKey),
      })
    },
  })
}

export function useDeleteAnnotation(): UseMutationResult<
  void,
  ApiError,
  { templateHash: string; serviceKey: string; annotationId: number }
> {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ templateHash, serviceKey, annotationId }) => {
      const result = await apiClient.DELETE(
        '/api/logs/signatures/{template_hash}/{service_key}/annotations/{annotation_id}',
        {
          params: {
            path: {
              template_hash: templateHash,
              service_key: serviceKey,
              annotation_id: annotationId,
            },
          },
        },
      )
      if (result.response.status === 204) return
      unwrap(result)
    },
    onSuccess: (_data, variables) => {
      void qc.invalidateQueries({
        queryKey: annotationKeys.list(variables.templateHash, variables.serviceKey),
      })
    },
  })
}
