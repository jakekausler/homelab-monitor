import { HostCpuTile } from '@/components/tiles/HostCpuTile'

export function OverviewPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Overview</h1>
        <p className="text-sm text-muted-foreground">
          Live system snapshot. More tiles land in upcoming stages.
        </p>
      </div>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
        <HostCpuTile />
      </div>
    </div>
  )
}
