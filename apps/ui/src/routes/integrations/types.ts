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

export type HaEntityRow = components['schemas']['HaEntityRow']
export type HaEntityRowsResponse = components['schemas']['HaEntityRowsResponse']
export type HaBatteryRow = components['schemas']['HaBatteryRow']
export type HaBatteryRowsResponse = components['schemas']['HaBatteryRowsResponse']
export type HaUpdateRow = components['schemas']['HaUpdateRow']
export type HaUpdateRowsResponse = components['schemas']['HaUpdateRowsResponse']
export type HaConfigEntryRow = components['schemas']['HaConfigEntryRow']
export type HaConfigEntryRowsResponse = components['schemas']['HaConfigEntryRowsResponse']
export type HaRepairRow = components['schemas']['HaRepairRow']
export type HaRepairRowsResponse = components['schemas']['HaRepairRowsResponse']

export type HaNotificationRow = components['schemas']['HaNotificationRow']
export type HaNotificationsResponse = components['schemas']['HaNotificationsResponse']

export type HaCadenceAutomationRow = components['schemas']['HaCadenceAutomationRow']
export type HaCadenceScriptRow = components['schemas']['HaCadenceScriptRow']
export type HaCadenceResponse = components['schemas']['HaCadenceResponse']
