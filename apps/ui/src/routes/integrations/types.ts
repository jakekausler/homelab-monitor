// SCAFFOLDING: ContainerRow fields populated incrementally across EPIC-003 stages.
// All fields except id+name are optional per D-CONTAINER-ROW-OPTIONAL.

export type ContainerRow = {
  id: string
  name: string
  image?: string | undefined
  status?: string | undefined
  cpu_pct?: number | undefined
  mem_mib?: number | undefined
  restart_count?: number | undefined
  exit_code?: number | undefined
  healthcheck?: string | undefined
  image_update?: 'available' | 'none' | undefined // STAGE-003-008/009
  probes?: string[] | undefined // STAGE-003-006/007
  logs_url?: string | undefined // Populated in STAGE-003-011 (per-container log viewer)
  actions_available?: boolean | undefined // STAGE-003-010
}
