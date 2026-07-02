import { useState } from 'react'
import type { JSX } from 'react'

import { useAutofixKillSwitch, useToggleAutofixKillSwitch } from '@/api/autofixSettings'
import { ApiError } from '@/api/client'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { ConfirmPhraseDialog } from '@/components/ConfirmPhraseDialog'

const DISABLE_PHRASE = 'disable auto-fix'
const ENABLE_PHRASE = 'enable auto-fix'

export function SettingsAutofixPage(): JSX.Element {
  const state = useAutofixKillSwitch()
  const toggle = useToggleAutofixKillSwitch()
  const [dialogOpen, setDialogOpen] = useState<boolean>(false)

  if (state.isLoading) {
    return (
      <div data-testid="settings-autofix-page" className="text-sm text-muted-foreground">
        Loading auto-fix settings…
      </div>
    )
  }
  if (state.error !== null || state.data === undefined) {
    return (
      <div data-testid="settings-autofix-page" className="text-sm text-destructive">
        Failed to load auto-fix settings.
      </div>
    )
  }

  const currentEnabled = state.data.enabled
  const expectedPhrase = currentEnabled ? DISABLE_PHRASE : ENABLE_PHRASE
  const targetEnabled = !currentEnabled
  const buttonLabel = currentEnabled ? 'Disable auto-fix' : 'Enable auto-fix'

  const errorMessage = toggle.error instanceof ApiError ? toggle.error.message : undefined

  const handleConfirm = (): void => {
    toggle.mutate(
      { enabled: targetEnabled, confirm_phrase: expectedPhrase },
      {
        onSuccess: () => {
          setDialogOpen(false)
        },
      },
    )
  }

  const lastResult = toggle.data

  return (
    <div data-testid="settings-autofix-page" className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Auto-fix kill switch</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <p className="text-sm">
            Current status:{' '}
            <span data-testid="autofix-status" className="font-medium">
              {currentEnabled ? 'Enabled' : 'Disabled'}
            </span>
          </p>
          {state.data.updated_at !== null ? (
            <p className="text-xs text-muted-foreground" data-testid="autofix-updated-at">
              Last changed: {state.data.updated_at}
            </p>
          ) : null}
          <Button
            data-testid="autofix-toggle"
            variant="destructive"
            onClick={() => setDialogOpen(true)}
            disabled={toggle.isPending}
          >
            {buttonLabel}
          </Button>
          {lastResult?.killed_inflight_run_id != null ? (
            <div
              data-testid="autofix-killed-run"
              className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-600 dark:text-amber-400"
            >
              Killed in-flight run {lastResult.killed_inflight_run_id}
            </div>
          ) : null}
          {lastResult?.unwind_warning != null ? (
            <div
              data-testid="autofix-unwind-warning"
              className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-600 dark:text-amber-400"
            >
              Warning: {lastResult.unwind_warning}
            </div>
          ) : null}
        </CardContent>
      </Card>
      <ConfirmPhraseDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        title={buttonLabel}
        body={
          currentEnabled
            ? 'Disabling auto-fix will SIGKILL any currently running fix and prevent new fixes from starting.'
            : 'Enabling auto-fix will allow matching runbooks to run automatically against your homelab.'
        }
        expectedPhrase={expectedPhrase}
        confirmLabel="I understand — proceed"
        onConfirm={handleConfirm}
        isPending={toggle.isPending}
        errorMessage={errorMessage}
      />
    </div>
  )
}
