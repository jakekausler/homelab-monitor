import { useState } from 'react'
import type { JSX } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { usePendingApprovals, type Approval } from '@/api/runbooks'

import { ApprovalPlanDialog } from './ApprovalPlanDialog'

function formatRelative(iso: string): string {
  const then = Date.parse(iso)
  if (Number.isNaN(then)) return iso
  const diffMs = Date.now() - then
  const seconds = Math.round(diffMs / 1000)
  if (seconds < 60) return `${String(seconds)}s ago`
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${String(minutes)} min ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${String(hours)} hr ago`
  const days = Math.round(hours / 24)
  return `${String(days)} days ago`
}

interface PendingApprovalsPanelProps {
  killSwitchEnabled: boolean
}

export function PendingApprovalsPanel({
  killSwitchEnabled,
}: PendingApprovalsPanelProps): JSX.Element {
  const approvals = usePendingApprovals()
  const [selectedApprovalId, setSelectedApprovalId] = useState<string | null>(null)

  return (
    <Card data-testid="pending-approvals-panel">
      <CardHeader>
        <CardTitle className="text-lg">Pending approvals</CardTitle>
      </CardHeader>
      <CardContent>
        {approvals.isLoading && (
          <p className="text-sm text-muted-foreground" data-testid="pending-approvals-loading">
            Loading approvals…
          </p>
        )}
        {approvals.error !== null && (
          <p
            role="alert"
            className="text-sm text-destructive"
            data-testid="pending-approvals-error"
          >
            {approvals.error.message}
          </p>
        )}
        {approvals.data !== undefined && approvals.data.items.length === 0 && (
          <p className="text-sm text-muted-foreground" data-testid="pending-approvals-empty">
            No pending approvals.
          </p>
        )}
        {approvals.data !== undefined && approvals.data.items.length > 0 && (
          <div className="overflow-x-auto">
            <table className="min-w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-muted-foreground">
                  <th className="pb-2 pr-4">Runbook</th>
                  <th className="pb-2 pr-4">Created</th>
                  <th className="pb-2 pr-4">Status</th>
                  <th className="pb-2 pr-4">Actions</th>
                </tr>
              </thead>
              <tbody>
                {approvals.data.items.map((a: Approval) => (
                  <tr key={a.id} data-testid={`pending-approvals-row-${a.id}`} className="border-t">
                    <td className="py-2 pr-4 font-mono text-xs">{a.runbook_id}</td>
                    <td className="py-2 pr-4">{formatRelative(a.created_at)}</td>
                    <td className="py-2 pr-4">
                      <span className="flex items-center gap-2">
                        <span>{a.status}</span>
                        {a.drift_detected && (
                          <span
                            data-testid={`pending-approvals-drift-${a.id}`}
                            className="rounded px-1.5 py-0.5 text-xs font-medium bg-amber-500/10 text-amber-700 dark:text-amber-300"
                          >
                            drift detected
                          </span>
                        )}
                      </span>
                    </td>
                    <td className="py-2 pr-4">
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => setSelectedApprovalId(a.id)}
                        data-testid={`pending-approvals-view-${a.id}`}
                      >
                        View &amp; Approve
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>

      <ApprovalPlanDialog
        approvalId={selectedApprovalId}
        killSwitchEnabled={killSwitchEnabled}
        onClose={() => setSelectedApprovalId(null)}
      />
    </Card>
  )
}
