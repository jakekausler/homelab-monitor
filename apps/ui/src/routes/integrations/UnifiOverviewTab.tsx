import type { JSX } from 'react'

// SCAFFOLDING: STAGE-020/022/023 fill this with real Unifi device/network/client data.
export function UnifiOverviewTab(): JSX.Element {
  return (
    <div className="h-full space-y-4 overflow-y-auto p-4">
      <div
        className="rounded-md border border-border bg-muted/30 p-6 text-center text-sm text-muted-foreground"
        role="status"
      >
        Unifi integration — coming in a later stage. No devices are being collected yet.
      </div>
    </div>
  )
}
