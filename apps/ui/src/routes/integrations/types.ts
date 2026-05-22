// ContainerRow is derived directly from the generated OpenAPI schema.
// Do not hand-maintain field types here — regenerate schema.ts instead via:
//   make openapi-export && pnpm --filter ui run generate-types

import type { components } from '@/api/schema'

export type ContainerRow = components['schemas']['ContainerRow'] & {
  // SCAFFOLDING: EPIC-003 fields not yet in backend schema; populated by future stages.
  image_update?: 'available' | 'none' | undefined // STAGE-003-008/009
  probes?: string[] | undefined // STAGE-003-006/007
  logs_url?: string | undefined // STAGE-003-011 (per-container log viewer)
  actions_available?: boolean | undefined // STAGE-003-010
}

export type DockerSuggestionRow = components['schemas']['DockerSuggestionRow']
export type DockerSuggestionListResponse = components['schemas']['DockerSuggestionListResponse']
