// STUB: STAGE-008-025 placeholder. Ops widgets land in STAGE-008-027.
import type { JSX } from 'react'

import { EmptyState } from '@/components/EmptyState'

export function SynologyOpsTab(): JSX.Element {
  return (
    <div className="h-full space-y-4 overflow-y-auto p-4">
      <EmptyState testId="synology-ops-empty">
        Backup, replication, updates, security, connections, and mount-health widgets — coming in
        STAGE-008-027.
      </EmptyState>
    </div>
  )
}
