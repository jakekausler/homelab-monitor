import type { JSX } from 'react'
import { Link, useParams } from '@tanstack/react-router'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { formatRelative, formatDuration, formatAbsolute } from '@/lib/relativeTime'
import { useNowTick } from '@/lib/useNowTick'
import { useRun, useRunFeedback, useRunTranscript } from '@/api/autofix-runs'
import { ModeBadge, OutcomeBadge } from './RunsTable'
import { TranscriptViewer } from './TranscriptViewer'
import { FeedbackList } from './FeedbackList'

export function RunDetailPage(): JSX.Element {
  const { run_id: runId } = useParams({ from: '/protected/autofix/history/$run_id' })
  const run = useRun(runId)
  const feedback = useRunFeedback(runId)
  const transcript = useRunTranscript(runId)
  const nowMs = useNowTick(1000)

  if (run.isLoading) {
    return <p className="p-4 text-sm text-muted-foreground">Loading run…</p>
  }
  if (run.isError || run.data === undefined) {
    return (
      <div className="p-4">
        <BackLink />
        <p role="alert" className="mt-4 text-sm text-destructive">
          Run not found.
        </p>
      </div>
    )
  }
  const r = run.data
  const isDryRun = r.mode === 'dry_run'
  const bannerClass = isDryRun
    ? 'border-yellow-500 bg-yellow-100 text-yellow-900'
    : 'border-blue-500 bg-blue-50 text-blue-900'

  return (
    <div className="space-y-4 p-4">
      <BackLink />

      <div
        className={`rounded-md border-2 p-3 ${bannerClass}`}
        data-testid={`mode-banner-${r.mode}`}
      >
        <strong>{isDryRun ? 'Dry-run — plan only' : 'Real run'}</strong>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="font-mono text-base">{r.runbook_path}</CardTitle>
        </CardHeader>
        <CardContent className="grid grid-cols-1 gap-3 text-sm md:grid-cols-2">
          <MetaRow label="Mode">
            <ModeBadge mode={r.mode} />
          </MetaRow>
          <MetaRow label="Outcome">
            <OutcomeBadge outcome={r.outcome} />
          </MetaRow>
          <MetaRow label="Exit code">{r.exit_code ?? '—'}</MetaRow>
          <MetaRow label="Started">
            {formatAbsolute(r.started_at)}{' '}
            <span className="text-muted-foreground">({formatRelative(r.started_at, nowMs)})</span>
          </MetaRow>
          <MetaRow label="Ended">
            {r.ended_at ? (
              <>
                {formatAbsolute(r.ended_at)}{' '}
                <span className="text-muted-foreground">({formatRelative(r.ended_at, nowMs)})</span>
              </>
            ) : (
              '—'
            )}
          </MetaRow>
          <MetaRow label="Duration">
            {r.duration_ms == null ? '—' : formatDuration(r.duration_ms / 1000)}
          </MetaRow>
          <MetaRow label="Runbook hash">
            <code className="text-xs">{(r.runbook_hash ?? '').slice(0, 12) || '—'}</code>
          </MetaRow>
          <MetaRow label="Initiated by">
            {r.initiated_by === 'operator' ? (
              <span>Operator</span>
            ) : r.alert_id ? (
              <span>Alert {r.alert_id.slice(0, 8)}</span>
            ) : (
              <span>Alert</span>
            )}
          </MetaRow>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Transcript</CardTitle>
        </CardHeader>
        <CardContent>
          {transcript.isLoading && (
            <p className="text-sm text-muted-foreground">Loading transcript…</p>
          )}
          {transcript.isError && (
            <p role="alert" className="text-sm text-destructive">
              {transcript.error?.code === 'transcript_dir_not_configured'
                ? 'Server misconfiguration: transcript directory not configured. Contact the operator.'
                : `Failed to load transcript: ${transcript.error?.message ?? 'unknown error'}`}
            </p>
          )}
          {transcript.data !== undefined &&
            (r.transcript_path === null ? (
              <p data-testid="transcript-absent">No transcript recorded for this run.</p>
            ) : (
              <TranscriptViewer transcript={transcript.data} />
            ))}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Feedback</CardTitle>
        </CardHeader>
        <CardContent>
          <FeedbackList items={feedback.data?.items ?? []} />
        </CardContent>
      </Card>
    </div>
  )
}

function BackLink(): JSX.Element {
  return (
    <Link
      to="/autofix/history"
      search={{
        runbook_id: undefined,
        mode: undefined,
        outcome: undefined,
        initiator: undefined,
        since: undefined,
        until: undefined,
        page: undefined,
      }}
      className="text-sm underline"
      data-testid="back-link"
    >
      ← Back to history
    </Link>
  )
}

function MetaRow({ label, children }: { label: string; children: React.ReactNode }): JSX.Element {
  return (
    <div className="flex items-baseline justify-between gap-2 border-b border-border/50 pb-1">
      <span className="text-xs uppercase text-muted-foreground">{label}</span>
      <span className="text-right">{children}</span>
    </div>
  )
}
