import { EmptyState } from '@/components/EmptyState'

export function RecentActionsPanel() {
  // SCAFFOLDING: STAGE-003-010 populates compose_actions audit data
  return (
    <div className="space-y-2">
      <h2 className="text-base font-semibold tracking-tight">Recent actions</h2>
      <EmptyState>No recent actions.</EmptyState>
    </div>
  )
}
