import { useState, type JSX } from 'react'
import { toast } from 'sonner'

import { usePiholeOverview } from '@/api/pihole'
import { useContainerLifecycleMutation, useListContainers } from '@/api/docker'
import type { ContainerLifecycleAction } from '@/api/docker'
import { ApiError } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { ConfirmPhraseDialog } from '@/components/ConfirmPhraseDialog'
import { EmptyState } from '@/components/EmptyState'

import { QueryState } from './QueryState'

const TRANSIENT_LABEL: Record<ContainerLifecycleAction, string> = {
  restart: 'Restarting',
  start: 'Starting',
  stop: 'Stopping',
}

export function PiholeVersionContainerWidget(): JSX.Element {
  const overview = usePiholeOverview()
  const containers = useListContainers()
  const mutation = useContainerLifecycleMutation()

  const [dialogOpen, setDialogOpen] = useState(false)
  const [pendingAction, setPendingAction] = useState<ContainerLifecycleAction | null>(null)
  const [errorMessage, setErrorMessage] = useState('')
  const [transientAction, setTransientAction] = useState<ContainerLifecycleAction | null>(null)

  const container = containers.data?.containers.find((c) => c.name === 'pihole-unbound')

  const statusStr = container?.status ?? 'unknown'
  const isRunning =
    statusStr.toLowerCase().includes('running') || statusStr.toLowerCase().includes('up')

  let statusBadgeVariant: 'ok' | 'muted' = 'muted'
  if (isRunning) statusBadgeVariant = 'ok'

  const openDialog = (action: ContainerLifecycleAction): void => {
    setPendingAction(action)
    setErrorMessage('')
    setTransientAction(null)
    setDialogOpen(true)
  }

  const handleConfirm = (): void => {
    if (pendingAction === null) return
    const action = pendingAction
    setErrorMessage('')
    mutation.mutate(
      { name: 'pihole-unbound', action, confirm_phrase: action },
      {
        onSuccess: () => {
          setDialogOpen(false)
          setTransientAction(action)
          toast.success(`pihole-unbound ${action} requested`)
        },
        onError: (err) => {
          setTransientAction(null)
          if (err instanceof ApiError && err.status === 400) {
            setErrorMessage(`Confirm phrase must be "${action}"`)
          } else {
            const msg = err instanceof Error ? err.message : 'Request failed'
            setErrorMessage(msg)
            toast.error(`Container ${action} failed: ${msg}`)
          }
        },
      },
    )
  }

  return (
    <div data-testid="pihole-version-container-widget" className="space-y-4 text-sm">
      <div>
        <h3 className="mb-2 font-semibold">Versions</h3>
        <QueryState
          result={overview}
          unavailableLabel="Pi-hole versions temporarily unavailable"
          renderData={(data) => {
            if (data.versions.length === 0) {
              return <EmptyState testId="pihole-versions-empty">No version data</EmptyState>
            }

            const updateSet = new Set(data.updates_available.map((u) => u.component))

            return (
              <div className="overflow-x-auto">
                <table className="w-full text-left text-sm">
                  <thead className="border-b border-border">
                    <tr>
                      <th className="px-2 py-1">Component</th>
                      <th className="px-2 py-1">Version</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {data.versions.map((v) => (
                      <tr key={v.component}>
                        <td className="px-2 py-1">{v.component}</td>
                        <td className="px-2 py-1">
                          <div className="flex items-center gap-2">
                            <span>{v.version}</span>
                            {updateSet.has(v.component) && (
                              <Badge variant="warn" className="text-xs">
                                update available
                              </Badge>
                            )}
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
          }}
        />
      </div>

      <div className="space-y-3">
        <h3 className="font-semibold">Container</h3>
        {containers.isPending ? (
          <p className="text-muted-foreground">Container status loading…</p>
        ) : containers.isError ? (
          <p className="text-muted-foreground">Container status unavailable</p>
        ) : (
          <div className="flex items-center gap-2">
            <Badge variant={statusBadgeVariant} className="text-xs">
              {container ? statusStr : 'unknown'}
              {transientAction && ` (${TRANSIENT_LABEL[transientAction]}…)`}
            </Badge>
          </div>
        )}

        <div className="flex flex-wrap gap-2">
          <Button
            data-testid="pihole-container-restart-button"
            variant="default"
            size="sm"
            onClick={() => openDialog('restart')}
            disabled={mutation.isPending}
          >
            Restart
          </Button>
          <Button
            data-testid="pihole-container-start-button"
            variant="default"
            size="sm"
            onClick={() => openDialog('start')}
            disabled={mutation.isPending}
          >
            Start
          </Button>
          <Button
            data-testid="pihole-container-stop-button"
            variant="destructive"
            size="sm"
            onClick={() => openDialog('stop')}
            disabled={mutation.isPending}
          >
            Stop
          </Button>
        </div>

        {pendingAction && (
          <ConfirmPhraseDialog
            open={dialogOpen}
            onOpenChange={setDialogOpen}
            expectedPhrase={pendingAction}
            title={`${pendingAction.charAt(0).toUpperCase() + pendingAction.slice(1)} pihole-unbound`}
            body={
              pendingAction === 'stop'
                ? 'This stops the combined Pi-hole + Unbound container — DNS resolution for the whole network will be interrupted.'
                : `${pendingAction.charAt(0).toUpperCase() + pendingAction.slice(1)} the pihole-unbound container?`
            }
            confirmLabel={pendingAction.charAt(0).toUpperCase() + pendingAction.slice(1)}
            onConfirm={handleConfirm}
            isPending={mutation.isPending}
            errorMessage={errorMessage}
          />
        )}
      </div>
    </div>
  )
}
