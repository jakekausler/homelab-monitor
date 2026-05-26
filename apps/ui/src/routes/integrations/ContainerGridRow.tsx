import { useEffect, useRef, useState } from 'react'
import { Link } from '@tanstack/react-router'
import { useQueryClient } from '@tanstack/react-query'

import type { ContainerRow } from './types'
import { StatusBadge, HealthcheckBadge } from './badges'
import { extractComposeBasename } from './composeBasename'
import { ProbesBadge } from './ProbesBadge'
import { ImageUpdateBadge } from './ImageUpdateBadge'
import { formatDigest } from '@/lib/digest'
import { Button } from '@/components/ui/button'
import { PullRestartModal } from '@/components/docker/PullRestartModal'
import {
  COMPOSE_ACTIVE_STATES,
  dockerImageUpdateQueryKeys,
  useImageUpdate,
  useListComposeActions,
} from '@/api/docker'
import { toast } from 'sonner'

interface ContainerGridRowProps {
  container: ContainerRow
}

export function ContainerGridRow({ container }: ContainerGridRowProps) {
  // Parent directory basename: "/.../homelab-monitor/docker-compose.yml" → "homelab-monitor"
  const composeBasename = extractComposeBasename(container.compose_file_path)

  return (
    <tr className="hover:bg-accent/30">
      <td
        className="px-3 py-2 text-xs text-muted-foreground"
        title={container.compose_file_path ?? undefined}
      >
        {composeBasename ?? '—'}
      </td>
      <td className="px-3 py-2 font-medium">{container.name}</td>
      <td className="px-3 py-2 text-xs text-muted-foreground">
        {container.status ? <StatusBadge status={container.status} /> : '—'}
      </td>
      <td
        className="px-3 py-2 text-right tabular-nums text-xs text-muted-foreground"
        title={
          container.restart_count != null && container.restart_count > 0
            ? `Cumulative: ${container.restart_count}`
            : undefined
        }
      >
        {container.restart_count_24h != null && container.restart_count_24h > 0
          ? container.restart_count_24h
          : '—'}
      </td>
      <td className="px-3 py-2 text-xs text-muted-foreground" title={container.image ?? undefined}>
        {formatDigest(container.image)}
      </td>
      <td className="px-3 py-2 text-xs text-muted-foreground">
        {container.cpu_pct != null ? `${container.cpu_pct.toFixed(1)}%` : '—'}
      </td>
      <td className="px-3 py-2 text-xs text-muted-foreground">
        {container.mem_mib != null ? `${container.mem_mib.toFixed(0)} MiB` : '—'}
      </td>
      <td className="px-3 py-2 text-xs text-muted-foreground">
        <ImageUpdateBadge containerName={container.name} />
      </td>
      <td className="px-3 py-2 text-xs text-muted-foreground">
        {container.healthcheck ? <HealthcheckBadge status={container.healthcheck} /> : '—'}
      </td>
      <td className="px-3 py-2 text-xs text-muted-foreground">
        <ProbesBadge containerName={container.name} />
      </td>
      <td className="px-3 py-2 text-xs text-muted-foreground">
        <Link
          to="/integrations/docker/containers/$name/logs"
          params={{ name: container.name }}
          className="text-primary hover:underline"
          data-testid={`logs-link-${container.name}`}
        >
          View logs →
        </Link>
      </td>
      <td className="px-3 py-2 text-xs text-muted-foreground">
        <ActionsCell container={container} />
      </td>
    </tr>
  )
}

export function ActionsCell({ container }: { container: ContainerRow }) {
  const [open, setOpen] = useState(false)
  const [optimisticActionId, setOptimisticActionId] = useState<number | null>(null)
  const invalidatedRef = useRef<Set<number>>(new Set())
  const imageUpdate = useImageUpdate(container.name)
  const list = useListComposeActions(container.name, 1)
  const queryClient = useQueryClient()

  // Derive label and confirm phrase based on image source
  const isLocalBuild = imageUpdate.data?.source === 'local_build'
  const actionLabel = isLocalBuild ? 'Rebuild & Restart' : 'Pull & Restart'
  // Confirm phrase stays "pull" for both flows — keeps API contract simple.
  // The action label/title in the modal differs, but the typed phrase is uniform.
  const confirmPhrase = 'pull'

  // Derive whether the optimistic action matches a row, and whether that row is terminal.
  const optimisticMatch =
    optimisticActionId !== null
      ? (list.data?.actions.find((a) => a.action_id === optimisticActionId) ?? null)
      : null
  const optimisticIsTerminal =
    optimisticMatch !== null && !COMPOSE_ACTIVE_STATES.has(optimisticMatch.state)

  // On terminal transition for the optimistic action: invalidate image-update caches
  // and show a toast. Run once per action_id via the ref guard — no setState in effect.
  useEffect(() => {
    if (optimisticActionId === null || !optimisticIsTerminal || optimisticMatch === null) return
    if (invalidatedRef.current.has(optimisticActionId)) return
    invalidatedRef.current.add(optimisticActionId)
    console.debug('[ActionsCell] terminal effect fired', {
      optimisticActionId,
      optimisticIsTerminal,
      state: optimisticMatch?.state,
    })
    void queryClient.invalidateQueries({ queryKey: dockerImageUpdateQueryKeys.summary })
    void queryClient.invalidateQueries({
      queryKey: dockerImageUpdateQueryKeys.detail(container.name),
    })
    // Toast feedback for the terminal outcome.
    const name = container.name
    switch (optimisticMatch.state) {
      case 'success':
        toast.success(`${actionLabel} succeeded for ${name}`)
        break
      case 'failed': {
        const reason = optimisticMatch.error_reason ?? 'unknown'
        toast.error(`${actionLabel} failed for ${name}: ${reason}`)
        break
      }
      case 'timeout':
        toast.error(`${actionLabel} timed out for ${name}`)
        break
      case 'killed':
        toast.warning(`${actionLabel} was cancelled for ${name}`)
        break
      default:
        break
    }
  }, [
    optimisticActionId,
    optimisticIsTerminal,
    optimisticMatch,
    queryClient,
    container.name,
    actionLabel,
  ])

  // Safety net: clear optimistic state after 30s in case polling glitches.
  useEffect(() => {
    if (optimisticActionId === null) return
    const timer = setTimeout(() => setOptimisticActionId(null), 30_000)
    return () => clearTimeout(timer)
  }, [optimisticActionId])

  const recentActive = list.data?.actions.find((a) => COMPOSE_ACTIVE_STATES.has(a.state)) ?? null
  // Show in-flight if EITHER:
  //   (a) optimistic action exists and hasn't reached terminal state yet, OR
  //   (b) the list shows any active row (covers state observed via polling alone).
  const optimisticActive = optimisticActionId !== null && !optimisticIsTerminal
  const showInFlight = optimisticActive || recentActive !== null

  if (showInFlight) {
    let phaseLabel = 'Pulling…'
    if (recentActive?.state === 'building') phaseLabel = 'Building…'
    else if (recentActive?.state === 'restarting') phaseLabel = 'Restarting…'
    return (
      <span className="inline-flex items-center gap-1 text-xs text-amber-700 dark:text-amber-300">
        <span className="h-2 w-2 animate-pulse rounded-full bg-amber-500" />
        {phaseLabel}
      </span>
    )
  }

  const updateAvailable = imageUpdate.data?.update_available === true

  const currentDigest =
    imageUpdate.data?.source === 'registry'
      ? (imageUpdate.data.last_local_digest ?? null)
      : (imageUpdate.data?.last_source_hash ?? null)
  const latestDigest =
    imageUpdate.data?.source === 'registry'
      ? (imageUpdate.data.last_registry_digest ?? null)
      : (imageUpdate.data?.baseline_source_hash ?? null)

  return (
    <>
      <Button
        variant="outline"
        size="sm"
        disabled={!updateAvailable}
        onClick={() => setOpen(true)}
        title={updateAvailable ? `${actionLabel} for this service` : 'No update available'}
      >
        {actionLabel}
      </Button>
      <PullRestartModal
        containerName={container.name}
        open={open}
        onOpenChange={setOpen}
        onActionStarted={(actionId) => {
          setOptimisticActionId(actionId)
        }}
        currentDigest={currentDigest}
        latestDigest={latestDigest}
        actionLabel={actionLabel}
        confirmPhrase={confirmPhrase}
      />
    </>
  )
}
