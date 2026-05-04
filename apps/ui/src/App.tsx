import type { JSX } from 'react'
import { Badge, Card } from '@tremor/react'

export function App(): JSX.Element {
  return (
    <div className="flex min-h-screen items-center justify-center bg-background p-6">
      <Card className="max-w-md">
        <h1 className="text-xl font-semibold text-foreground">homelab-monitor</h1>
        <p className="mt-1 text-sm text-tremor-content dark:text-dark-tremor-content">
          status: scaffolding &bull; EPIC-001 STAGE-001-002
        </p>
        <div className="mt-4 flex items-center gap-2">
          <Badge color="blue">dev</Badge>
          <span className="text-xs text-tremor-content dark:text-dark-tremor-content">
            dark mode active
          </span>
        </div>
        <p className="mt-6 font-mono text-xs text-tremor-content dark:text-dark-tremor-content">
          run <span className="text-foreground">make verify</span> to confirm green pipeline
        </p>
      </Card>
    </div>
  )
}
