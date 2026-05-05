import type { JSX } from 'react'

export function App(): JSX.Element {
  return (
    <div className="flex min-h-screen items-center justify-center bg-background p-6">
      <div className="max-w-md rounded-lg border border-slate-700 bg-slate-800 p-6 shadow-sm">
        <h1 className="text-xl font-semibold text-foreground">homelab-monitor</h1>
        <p className="mt-1 text-sm text-slate-400">
          status: scaffolding &bull; EPIC-001 STAGE-001-002
        </p>
        <div className="mt-4 flex items-center gap-2">
          <span className="inline-flex items-center rounded-md bg-blue-500/20 px-2 py-1 text-xs font-medium text-blue-300 ring-1 ring-blue-500/30 ring-inset">
            dev
          </span>
          <span className="text-xs text-slate-400">dark mode active</span>
        </div>
        <p className="mt-6 font-mono text-xs text-slate-400">
          run <span className="text-foreground">make verify</span> to confirm green pipeline
        </p>
      </div>
    </div>
  )
}
