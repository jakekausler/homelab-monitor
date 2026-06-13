// ContainerRow is derived directly from the generated OpenAPI schema.
// Do not hand-maintain field types here — regenerate schema.ts instead via:
//   make openapi-export && pnpm --filter ui run generate-types

import type { components } from '@/api/schema'

export type ContainerRow = components['schemas']['ContainerRow']

export type DockerSuggestionRow = components['schemas']['DockerSuggestionRow']
export type DockerSuggestionListResponse = components['schemas']['DockerSuggestionListResponse']

export type HaSummaryResponse = components['schemas']['HaSummaryResponse']
export type HaEntitiesSummary = components['schemas']['HaEntitiesSummary']
export type HaBatterySummary = components['schemas']['HaBatterySummary']
export type HaUpdatesSummary = components['schemas']['HaUpdatesSummary']
export type HaConfigEntriesSummary = components['schemas']['HaConfigEntriesSummary']
