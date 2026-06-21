import type { JSX } from 'react'

export function NetworkClientsTab(): JSX.Element {
  return (
    <div className="h-full overflow-y-auto p-4">
      <div
        className="rounded-md border border-border bg-muted/30 p-6 text-center text-sm text-muted-foreground"
        role="status"
      >
        Client inventory arrives in STAGE-007-022.
      </div>
    </div>
  )
}
