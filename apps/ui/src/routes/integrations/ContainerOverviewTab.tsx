import { useEffect, useRef, useState } from 'react'
import type { JSX } from 'react'
import { useParams } from '@tanstack/react-router'
import { useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'

import {
  dockerImageUpdateQueryKeys,
  dockerQueryKeys,
  useImageUpdate,
  useListContainers,
} from '@/api/docker'
import { useMetricsRange } from '@/api/queries'
import { Button } from '@/components/ui/button'
import { ErrorDisplay } from '@/components/ErrorDisplay'
import { PullRestartModal } from '@/components/docker/PullRestartModal'
import { RecentCrashesSection } from '@/components/docker/RecentCrashesSection'
import { Sparkline } from '@/components/tiles/Sparkline'
import { formatDigest } from '@/lib/digest'
import { formatRelative } from '@/lib/relativeTime'
import { formatSourceHash } from '@/lib/sourceHash'

const SPARKLINE_LOOKBACK_S = 900 // 15 minutes
const SPARKLINE_STEP = '15s'
const SPARKLINE_CAPACITY = 60 // 900 / 15
const PULL_RESTART_TIMEOUT_MS = 30_000

function rangeWindow(): { start: string; end: string } {
  const endMs = Date.now()
  const startMs = endMs - SPARKLINE_LOOKBACK_S * 1000
  return {
    start: new Date(startMs).toISOString(),
    end: new Date(endMs).toISOString(),
  }
}

// Duplicated from HostCpuTile.tsx — when a 3rd consumer appears, hoist to apps/ui/src/lib/vmRange.ts.
function buildSeriesFromVMValues(
  values: ReadonlyArray<ReadonlyArray<number | string>>,
): number[] | null {
  const parsed: number[] = []
  for (const pair of values) {
    if (pair.length < 2) continue
    const raw = pair[1]!
    const n = typeof raw === 'string' ? Number(raw) : raw
    if (Number.isFinite(n)) parsed.push(n)
  }
  if (parsed.length === 0) return null
  if (parsed.length >= SPARKLINE_CAPACITY) {
    return parsed.slice(parsed.length - SPARKLINE_CAPACITY)
  }
  // Pad start with the first sample so the sparkline starts visually steady.
  const padded = Array<number>(SPARKLINE_CAPACITY - parsed.length).fill(parsed[0]!) // safe: length > 0 checked above
  return [...padded, ...parsed]
}

function cpuExpr(name: string): string {
  return `rate(container_cpu_usage_seconds_total{name="${name}"}[1m]) * 100`
}

function memExpr(name: string): string {
  return `container_memory_usage_bytes{name="${name}"} / 1024 / 1024`
}

export function ContainerOverviewTab(): JSX.Element {
  const { name } = useParams({ strict: false })
  const containerName = typeof name === 'string' && name.length > 0 ? name : null
  if (!containerName) {
    return <div className="text-sm text-red-600">No container name provided.</div>
  }
  return <OverviewBody name={containerName} />
}

interface OverviewBodyProps {
  name: string
}

function OverviewBody({ name }: OverviewBodyProps): JSX.Element {
  const imageResult = useImageUpdate(name)
  const listResult = useListContainers()
  const row = listResult.data?.containers.find((c) => c.name === name) ?? null

  const [{ start: rangeStart, end: rangeEnd }] = useState(() => rangeWindow())
  const cpuRange = useMetricsRange(cpuExpr(name), rangeStart, rangeEnd, SPARKLINE_STEP)
  const memRange = useMetricsRange(memExpr(name), rangeStart, rangeEnd, SPARKLINE_STEP)

  const cpuSeries = cpuRange.data?.data.result[0]
    ? (buildSeriesFromVMValues(cpuRange.data.data.result[0].values) ?? [])
    : []
  const memSeries = memRange.data?.data.result[0]
    ? (buildSeriesFromVMValues(memRange.data.data.result[0].values) ?? [])
    : []

  // Pull & Restart state
  const qc = useQueryClient()
  const [pullRestartOpen, setPullRestartOpen] = useState(false)
  const [actionInProgress, setActionInProgress] = useState(false)
  const invalidatedRef = useRef(false)
  const pullRestartTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    return () => {
      if (pullRestartTimeoutRef.current !== null) {
        clearTimeout(pullRestartTimeoutRef.current)
      }
    }
  }, [])

  const handlePullRestartStart = () => {
    setActionInProgress(true)
    invalidatedRef.current = false

    pullRestartTimeoutRef.current = setTimeout(() => {
      if (!invalidatedRef.current) {
        void qc.invalidateQueries({ queryKey: dockerImageUpdateQueryKeys.detail(name) })
        void qc.invalidateQueries({ queryKey: dockerQueryKeys.containers })
        invalidatedRef.current = true
        toast.success('Action completed')
      }
      setActionInProgress(false)
      pullRestartTimeoutRef.current = null
    }, PULL_RESTART_TIMEOUT_MS)
  }

  return (
    <div className="space-y-6">
      {/* Block 1: Image update detail */}
      <section aria-label="Image update" className="space-y-3">
        {imageResult.isError && <ErrorDisplay error={imageResult.error} />}
        {imageResult.isPending && <div className="text-sm text-muted-foreground">Loading…</div>}
        {imageResult.data && (
          <>
            <dl className="grid grid-cols-1 gap-2 rounded-md border border-border bg-card p-3 text-sm md:grid-cols-2">
              {imageResult.data.source === 'local_build' ? (
                <>
                  <div className="md:col-span-2">
                    <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                      Source
                    </dt>
                    <dd>Local build</dd>
                  </div>
                  <div>
                    <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                      Build context
                    </dt>
                    <dd className="font-mono text-xs">
                      {imageResult.data.build_context_path ?? '—'}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                      Compose service
                    </dt>
                    <dd className="font-mono text-xs">{imageResult.data.compose_service ?? '—'}</dd>
                  </div>
                  <div>
                    <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                      Current source hash
                    </dt>
                    <dd className="font-mono text-xs">
                      {imageResult.data.last_source_hash
                        ? formatSourceHash(imageResult.data.last_source_hash)
                        : '—'}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                      Baseline source hash
                    </dt>
                    <dd className="font-mono text-xs">
                      {imageResult.data.baseline_source_hash
                        ? formatSourceHash(imageResult.data.baseline_source_hash)
                        : '—'}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                      Update available
                    </dt>
                    <dd>{imageResult.data.update_available ? 'yes' : 'no'}</dd>
                  </div>
                  <div>
                    <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                      Last checked
                    </dt>
                    <dd title={imageResult.data.last_checked_at ?? undefined}>
                      {formatRelative(imageResult.data.last_checked_at)}
                    </dd>
                  </div>
                </>
              ) : (
                <>
                  <div className="md:col-span-2">
                    <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                      Source
                    </dt>
                    <dd>Registry</dd>
                  </div>
                  <div className="md:col-span-2">
                    <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                      Image ref
                    </dt>
                    <dd className="font-mono text-xs">{imageResult.data.last_image_ref ?? '—'}</dd>
                  </div>
                  <div>
                    <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                      Current digest
                    </dt>
                    <dd className="font-mono text-xs">
                      {imageResult.data.last_local_digest
                        ? formatDigest(imageResult.data.last_local_digest)
                        : '—'}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                      Latest digest
                    </dt>
                    <dd className="font-mono text-xs">
                      {imageResult.data.last_registry_digest
                        ? formatDigest(imageResult.data.last_registry_digest)
                        : '—'}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                      Update available
                    </dt>
                    <dd>{imageResult.data.update_available ? 'yes' : 'no'}</dd>
                  </div>
                  <div>
                    <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                      Last checked
                    </dt>
                    <dd title={imageResult.data.last_checked_at ?? undefined}>
                      {formatRelative(imageResult.data.last_checked_at)}
                    </dd>
                  </div>
                </>
              )}
            </dl>
            {imageResult.data.update_available && (
              <>
                <Button
                  onClick={() => setPullRestartOpen(true)}
                  disabled={actionInProgress}
                  data-testid="pull-restart-button"
                >
                  {imageResult.data.source === 'local_build'
                    ? 'Rebuild & Restart'
                    : 'Pull & Restart'}
                </Button>
                <PullRestartModal
                  containerName={name}
                  open={pullRestartOpen}
                  onOpenChange={setPullRestartOpen}
                  onActionStarted={handlePullRestartStart}
                  currentDigest={
                    imageResult.data.source === 'local_build'
                      ? (imageResult.data.last_source_hash ?? null)
                      : (imageResult.data.last_local_digest ?? null)
                  }
                  latestDigest={
                    imageResult.data.source === 'local_build'
                      ? (imageResult.data.baseline_source_hash ?? null)
                      : (imageResult.data.last_registry_digest ?? null)
                  }
                  actionLabel={
                    imageResult.data.source === 'local_build'
                      ? 'Rebuild & Restart'
                      : 'Pull & Restart'
                  }
                  confirmPhrase={imageResult.data.source === 'local_build' ? 'rebuild' : 'pull'}
                />
              </>
            )}
          </>
        )}
      </section>

      {/* Block 2: Container metadata */}
      {row && (
        <section
          aria-label="Container metadata"
          className="rounded-md border border-border bg-card p-3"
        >
          <h2 className="mb-2 text-sm font-semibold">Container</h2>
          <dl className="grid grid-cols-1 gap-2 text-sm md:grid-cols-2">
            <div>
              <dt className="text-xs uppercase tracking-wide text-muted-foreground">Image</dt>
              <dd className="font-mono text-xs" title={row.image ?? undefined}>
                {row.image ?? '—'}
              </dd>
            </div>
            <div>
              <dt className="text-xs uppercase tracking-wide text-muted-foreground">Status</dt>
              <dd>{row.status ?? '—'}</dd>
            </div>
            <div>
              <dt className="text-xs uppercase tracking-wide text-muted-foreground">Healthcheck</dt>
              <dd>{row.healthcheck ?? '—'}</dd>
            </div>
            <div>
              <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                Compose project
              </dt>
              <dd>{row.compose_project ?? '—'}</dd>
            </div>
            <div>
              <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                Compose service
              </dt>
              <dd>{row.compose_service ?? '—'}</dd>
            </div>
            <div className="md:col-span-2">
              <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                Compose file
              </dt>
              <dd className="font-mono text-xs" title={row.compose_file_path ?? undefined}>
                {row.compose_file_path ?? '—'}
              </dd>
            </div>
          </dl>
        </section>
      )}

      {/* Block 3: Restart/exit history */}
      {row && (
        <section
          aria-label="Restart history"
          className="rounded-md border border-border bg-card p-3"
        >
          <h2 className="mb-2 text-sm font-semibold">Restart history</h2>
          <dl className="grid grid-cols-2 gap-2 text-sm">
            <div>
              <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                Restarts (24h)
              </dt>
              <dd className="tabular-nums">{row.restart_count_24h ?? 0}</dd>
            </div>
            <div>
              <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                Restarts (cumulative)
              </dt>
              <dd className="tabular-nums">{row.restart_count ?? 0}</dd>
            </div>
            <div>
              <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                Last exit code
              </dt>
              <dd className="tabular-nums">{row.exit_code ?? '—'}</dd>
            </div>
            <div>
              <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                Last recreated
              </dt>
              <dd title={row.recreated_at ?? undefined}>{formatRelative(row.recreated_at)}</dd>
            </div>
          </dl>
        </section>
      )}

      {/* Block 4: Resource usage */}
      {row && (
        <section
          aria-label="Resource usage"
          className="rounded-md border border-border bg-card p-3"
        >
          <h2 className="mb-2 text-sm font-semibold">Resource usage</h2>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            <div>
              <div className="text-xs uppercase tracking-wide text-muted-foreground">CPU</div>
              <div className="text-2xl font-semibold tabular-nums" data-testid="overview-cpu">
                {row.cpu_pct != null ? `${row.cpu_pct.toFixed(1)}%` : '—'}
              </div>
              {cpuRange.isPending ? (
                <div
                  className="h-[50px] w-[240px] animate-pulse rounded bg-muted/40"
                  data-testid="cpu-sparkline-loading"
                />
              ) : cpuSeries.length > 0 ? (
                <Sparkline
                  values={cpuSeries}
                  width={240}
                  height={50}
                  ariaLabel={`CPU history for ${name}`}
                />
              ) : (
                <span className="text-xs text-muted-foreground" data-testid="cpu-sparkline-empty">
                  No history available
                </span>
              )}
            </div>
            <div>
              <div className="text-xs uppercase tracking-wide text-muted-foreground">Memory</div>
              <div className="text-2xl font-semibold tabular-nums" data-testid="overview-mem">
                {row.mem_mib != null ? `${row.mem_mib.toFixed(0)} MiB` : '—'}
              </div>
              {memRange.isPending ? (
                <div className="h-[50px] w-[240px] animate-pulse rounded bg-muted/40" />
              ) : memSeries.length > 0 ? (
                <Sparkline
                  values={memSeries}
                  width={240}
                  height={50}
                  ariaLabel={`Memory history for ${name}`}
                />
              ) : (
                <span className="text-xs text-muted-foreground">No history available</span>
              )}
            </div>
          </div>
        </section>
      )}

      {/* Block 5: Recent crashes (STAGE-004-032) */}
      <RecentCrashesSection containerName={name} />
    </div>
  )
}
