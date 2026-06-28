// STUB: STAGE-008-025 placeholder. Hardware widgets land in STAGE-008-026.
import type { JSX } from 'react'

import { EmptyState } from '@/components/EmptyState'

export function SynologyHardwareTab(): JSX.Element {
  return (
    <div className="h-full space-y-4 overflow-y-auto p-4">
      <EmptyState testId="synology-hardware-empty">
        Storage, disk/SMART, RAID, system, and UPS widgets — coming in STAGE-008-026.
      </EmptyState>
    </div>
  )
}
