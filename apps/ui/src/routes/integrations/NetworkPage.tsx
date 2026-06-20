import type { JSX } from 'react'

// SCAFFOLDING: STAGE-021 fill this (promote to a layout if child tabs are needed).
export function NetworkPage(): JSX.Element {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Network</h1>
        <p className="text-sm text-muted-foreground">
          Network monitoring lands in an upcoming stage.
        </p>
      </div>
      <div
        className="rounded-md border border-border bg-muted/30 p-6 text-center text-sm text-muted-foreground"
        role="status"
      >
        Network — not yet configured.
      </div>
    </div>
  )
}
