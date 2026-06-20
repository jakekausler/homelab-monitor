import type { RunState } from '@/components/crons/badges'

export type RunSearchSchema = {
  state?: RunState | undefined
  cursor?: string | undefined
}
