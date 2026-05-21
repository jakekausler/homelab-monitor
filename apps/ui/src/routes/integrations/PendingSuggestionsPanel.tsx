import { EmptyState } from '@/components/EmptyState'

export function PendingSuggestionsPanel() {
  // SCAFFOLDING: STAGE-003-005 populates suggestions; EPIC-011 will subsume with global Suggestions inbox
  return (
    <div className="space-y-2">
      <h2 className="text-base font-semibold tracking-tight">Pending suggestions</h2>
      <EmptyState>No pending suggestions.</EmptyState>
    </div>
  )
}
