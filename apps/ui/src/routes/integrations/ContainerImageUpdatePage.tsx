import { Link, useParams } from '@tanstack/react-router'

import { useImageUpdate } from '@/api/docker'
import { ErrorDisplay } from '@/components/ErrorDisplay'
import { formatDigest } from '@/lib/digest'
import { formatSourceHash } from '@/lib/sourceHash'
import { formatRelative } from '@/lib/relativeTime'

export function ContainerImageUpdatePage() {
  const { name } = useParams({ strict: false })
  const containerName = typeof name === 'string' && name.length > 0 ? name : null
  const result = useImageUpdate(containerName ?? '')

  if (!containerName) {
    return (
      <div className="space-y-4">
        <div className="text-sm text-red-600">
          No container name provided. Navigate from the Docker containers list.
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <div>
        <Link to="/integrations/docker" className="text-xs text-muted-foreground hover:underline">
          ← Back to Docker integration
        </Link>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight">
          Image update for {containerName}
        </h1>
      </div>

      {result.isError && <ErrorDisplay error={result.error} />}
      {result.isPending && (
        <div className="text-sm text-muted-foreground">Loading image-update state…</div>
      )}
      {result.data && result.data.source === 'local_build' && (
        <dl className="grid grid-cols-1 gap-2 rounded-md border border-border bg-card p-3 text-sm md:grid-cols-2">
          <div className="md:col-span-2">
            <dt className="text-xs uppercase tracking-wide text-muted-foreground">Source</dt>
            <dd>Local build</dd>
          </div>
          <div>
            <dt className="text-xs uppercase tracking-wide text-muted-foreground">
              Compose service
            </dt>
            <dd className="font-mono text-xs">{result.data.compose_service ?? '—'}</dd>
          </div>
          <div>
            <dt className="text-xs uppercase tracking-wide text-muted-foreground">
              Build context path
            </dt>
            <dd className="font-mono text-xs">{result.data.build_context_path ?? '—'}</dd>
          </div>
          <div>
            <dt className="text-xs uppercase tracking-wide text-muted-foreground">
              Update available
            </dt>
            <dd>{result.data.update_available ? 'yes' : 'no'}</dd>
          </div>
          <div>
            <dt className="text-xs uppercase tracking-wide text-muted-foreground">
              Last source hash
            </dt>
            <dd className="font-mono text-xs" title={result.data.last_source_hash ?? undefined}>
              {formatSourceHash(result.data.last_source_hash ?? null)}
            </dd>
          </div>
          {result.data.update_available &&
            result.data.baseline_source_hash &&
            result.data.baseline_source_hash !== result.data.last_source_hash && (
              <div>
                <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                  Baseline source hash
                </dt>
                <dd className="font-mono text-xs" title={result.data.baseline_source_hash ?? ''}>
                  {formatSourceHash(result.data.baseline_source_hash)}
                </dd>
              </div>
            )}
          <div>
            <dt className="text-xs uppercase tracking-wide text-muted-foreground">Last checked</dt>
            <dd
              title={result.data.last_checked_at ? String(result.data.last_checked_at) : undefined}
            >
              {formatRelative(String(result.data.last_checked_at))}
            </dd>
          </div>
          <div>
            <dt className="text-xs uppercase tracking-wide text-muted-foreground">
              Check failed at
            </dt>
            <dd title={result.data.check_failed_at ?? undefined}>
              {formatRelative(result.data.check_failed_at)}
            </dd>
          </div>
          {result.data.check_error_reason && (
            <div className="md:col-span-2">
              <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                Check error reason
              </dt>
              <dd className="text-red-700">{result.data.check_error_reason}</dd>
            </div>
          )}
        </dl>
      )}
      {result.data && result.data.source === 'registry' && (
        <dl className="grid grid-cols-1 gap-2 rounded-md border border-border bg-card p-3 text-sm md:grid-cols-2">
          <div>
            <dt className="text-xs uppercase tracking-wide text-muted-foreground">Image ref</dt>
            <dd className="font-mono text-xs">{result.data.last_image_ref ?? '—'}</dd>
          </div>
          <div>
            <dt className="text-xs uppercase tracking-wide text-muted-foreground">
              Update available
            </dt>
            <dd>{result.data.update_available ? 'yes' : 'no'}</dd>
          </div>
          <div>
            <dt className="text-xs uppercase tracking-wide text-muted-foreground">
              Current digest
            </dt>
            <dd className="font-mono text-xs" title={result.data.last_local_digest ?? undefined}>
              {formatDigest(result.data.last_local_digest)}
            </dd>
          </div>
          <div>
            <dt className="text-xs uppercase tracking-wide text-muted-foreground">Latest digest</dt>
            <dd className="font-mono text-xs" title={result.data.last_registry_digest ?? undefined}>
              {formatDigest(result.data.last_registry_digest)}
            </dd>
          </div>
          <div>
            <dt className="text-xs uppercase tracking-wide text-muted-foreground">Last checked</dt>
            <dd title={result.data.last_checked_at ?? undefined}>
              {formatRelative(result.data.last_checked_at)}
            </dd>
          </div>
          <div>
            <dt className="text-xs uppercase tracking-wide text-muted-foreground">
              Check failed at
            </dt>
            <dd title={result.data.check_failed_at ?? undefined}>
              {formatRelative(result.data.check_failed_at)}
            </dd>
          </div>
          {result.data.check_error_reason && (
            <div className="md:col-span-2">
              <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                Check error reason
              </dt>
              <dd className="text-red-700">{result.data.check_error_reason}</dd>
            </div>
          )}
        </dl>
      )}
    </div>
  )
}
