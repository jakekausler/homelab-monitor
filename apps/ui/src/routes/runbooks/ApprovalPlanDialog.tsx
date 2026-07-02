import { useState } from 'react'
import type { JSX } from 'react'

import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { ConfirmPhraseDialog } from '@/components/ConfirmPhraseDialog'
import { ApiError } from '@/api/client'
import { useApprovalPlan, useApproveApproval, useRejectApproval } from '@/api/runbooks'

interface ApprovalPlanDialogProps {
  approvalId: string | null
  killSwitchEnabled: boolean
  onClose: () => void
}

export function friendlyApproveError(err: unknown): string {
  if (!(err instanceof ApiError)) return err instanceof Error ? err.message : 'Unknown error'
  switch (err.code) {
    case 'runbook_changed_since_plan':
      return 'Runbook changed since plan generated — reject and re-run against a fresh alert.'
    case 'runbook_missing':
      return 'Runbook is no longer registered.'
    case 'kill_switch':
      return 'Auto-fix is disabled. Enable it in Settings first.'
    case 'approval_not_pending':
      return 'This approval is no longer pending.'
    case 'rate_limit':
      return 'reason: rate limit exceeded'
    case 'cooldown':
      return 'reason: cooldown active'
    case 'already_running':
      return 'reason: another run for this runbook is already in progress'
    case 'allow_list':
      return 'reason: not on the allow list'
    case 'ambiguous_match':
      return 'reason: multiple candidate runbooks matched'
    case 'claim_error':
      return 'reason: could not claim the run row'
    default:
      return err.message
  }
}

export function ApprovalPlanDialog({
  approvalId,
  killSwitchEnabled,
  onClose,
}: ApprovalPlanDialogProps): JSX.Element {
  const [confirmOpen, setConfirmOpen] = useState(false)
  const plan = useApprovalPlan(approvalId)
  const approve = useApproveApproval()
  const reject = useRejectApproval()

  const handleReject = (): void => {
    if (approvalId === null) return
    reject.mutate(
      { approvalId },
      {
        onSuccess: () => {
          onClose()
        },
      },
    )
  }

  const handleApproveConfirmed = (): void => {
    if (approvalId === null) return
    approve.mutate(
      { approvalId, confirm_phrase: 'approve' },
      {
        onSuccess: () => {
          setConfirmOpen(false)
          onClose()
        },
      },
    )
  }

  const dialogOpen = approvalId !== null
  const rejectError = reject.error !== null ? friendlyApproveError(reject.error) : null
  const approveError = approve.error !== null ? friendlyApproveError(approve.error) : null

  return (
    <>
      <Dialog
        open={dialogOpen}
        onOpenChange={(open) => {
          if (!open) {
            setConfirmOpen(false)
            onClose()
          }
        }}
      >
        <DialogContent className="max-h-[90vh] w-[95vw] overflow-y-auto sm:max-w-3xl">
          <DialogHeader>
            <DialogTitle>Approval plan</DialogTitle>
            <DialogDescription>Review the dry-run plan before approving.</DialogDescription>
          </DialogHeader>

          {plan.isLoading && (
            <p className="text-sm text-muted-foreground" data-testid="approval-plan-loading">
              Loading plan…
            </p>
          )}
          {plan.error !== null && (
            <p role="alert" className="text-sm text-destructive" data-testid="approval-plan-error">
              {friendlyApproveError(plan.error)}
            </p>
          )}
          {plan.data !== undefined && (
            <div className="space-y-3">
              <div className="grid grid-cols-1 gap-1 text-xs text-muted-foreground sm:grid-cols-2">
                <div>
                  Runbook: <code>{plan.data.runbook_id}</code>
                </div>
                <div>
                  Dry run: <code>{plan.data.dry_run_id}</code>
                </div>
                <div>
                  Exit code:{' '}
                  <code>{plan.data.exit_code === null ? '—' : String(plan.data.exit_code)}</code>
                </div>
                <div className="truncate">
                  Transcript: <code>{plan.data.transcript_path}</code>
                </div>
              </div>

              <div className="flex justify-end">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    if (typeof navigator !== 'undefined' && navigator.clipboard) {
                      navigator.clipboard.writeText(plan.data.plan_text).catch(() => {
                        /* silent */
                      })
                    }
                  }}
                  data-testid="approval-plan-copy"
                >
                  Copy plan
                </Button>
              </div>

              <pre
                className="max-h-[50vh] overflow-auto rounded border bg-muted p-3 text-xs whitespace-pre-wrap break-words"
                data-testid="approval-plan-text"
              >
                {plan.data.plan_text}
              </pre>
            </div>
          )}

          {rejectError !== null && (
            <p
              role="alert"
              className="text-sm text-destructive"
              data-testid="approval-reject-error"
            >
              {rejectError}
            </p>
          )}
          {approveError !== null && (
            <p
              role="alert"
              className="text-sm text-destructive"
              data-testid="approval-approve-error"
            >
              {approveError}
            </p>
          )}

          <DialogFooter className="gap-2 sm:gap-0">
            <Button
              variant="outline"
              onClick={handleReject}
              disabled={reject.isPending || approvalId === null}
              data-testid="approval-reject"
            >
              {reject.isPending ? 'Rejecting…' : 'Reject'}
            </Button>
            <Button
              variant="destructive"
              onClick={() => setConfirmOpen(true)}
              disabled={!killSwitchEnabled || approvalId === null || plan.data === undefined}
              data-testid="approval-approve"
            >
              Approve
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <ConfirmPhraseDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title="Approve auto-fix run"
        body="This will execute the runbook against the real target. Type approve to confirm."
        expectedPhrase="approve"
        confirmLabel="Approve"
        onConfirm={handleApproveConfirmed}
        isPending={approve.isPending}
        errorMessage={approveError ?? undefined}
      />
    </>
  )
}
