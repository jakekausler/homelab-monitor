import type { DockerSuggestionRow } from './types'

const REASON_LABELS: Record<string, string> = {
  no_homelab_monitor_label: 'No labels',
  disabled_profile: 'Disabled profile',
  label_collision: 'Label collision',
}

function ReasonPill({ reason, kind }: { reason: string; kind: string }) {
  // Label collisions get their own visually distinct pill.
  const isCollision = kind === 'docker_label_collision'
  const text = isCollision ? 'Label collision' : (REASON_LABELS[reason] ?? reason)
  return (
    <span
      className={
        isCollision
          ? 'inline-flex items-center rounded-full bg-destructive/15 px-2 py-0.5 text-xs font-medium text-destructive'
          : 'inline-flex items-center rounded-full bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground'
      }
      data-testid={isCollision ? 'collision-pill' : 'reason-pill'}
    >
      {text}
    </span>
  )
}

export function SuggestionCard({ suggestion }: { suggestion: DockerSuggestionRow }) {
  const labels = suggestion.labels ?? {}
  // Only render homelab-monitor.* labels — vendor labels (com.docker.compose.*,
  // org.opencontainers.image.*, etc.) are noise in this context.
  const labelEntries = Object.entries(labels)
    .filter(([key]) => key.startsWith('homelab-monitor.'))
    .slice(0, 6)

  return (
    <article
      className="space-y-2 rounded-md border border-border bg-card p-4"
      data-testid="suggestion-card"
      data-suggestion-id={suggestion.id}
    >
      <header className="flex items-center justify-between">
        <h3 className="text-sm font-semibold tracking-tight">{suggestion.container_name}</h3>
        <ReasonPill reason={suggestion.detection_reason} kind={suggestion.kind} />
      </header>
      <p className="truncate text-xs text-muted-foreground" title={suggestion.image_ref}>
        {suggestion.image_ref}
      </p>
      {labelEntries.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {labelEntries.map(([k, v]) => (
            <span
              key={k}
              className="inline-flex items-center rounded-sm bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground"
            >
              {k}={v}
            </span>
          ))}
        </div>
      )}
      {suggestion.compose_file_path && (
        <p
          className="truncate text-xs text-muted-foreground font-mono"
          data-testid="compose-file-path"
          title={suggestion.compose_file_path}
        >
          {suggestion.compose_file_path}
        </p>
      )}
      {/* SCAFFOLDING: STAGE-003-010 wires Accept/Customize/Ignore. EPIC-011 will subsume with global Suggestions inbox. */}
      <div className="flex gap-2 pt-2">
        <button
          type="button"
          disabled
          aria-disabled="true"
          className="rounded-md border border-border px-3 py-1 text-xs disabled:cursor-not-allowed disabled:opacity-50"
        >
          Accept
        </button>
        <button
          type="button"
          disabled
          aria-disabled="true"
          className="rounded-md border border-border px-3 py-1 text-xs disabled:cursor-not-allowed disabled:opacity-50"
        >
          Customize
        </button>
        <button
          type="button"
          disabled
          aria-disabled="true"
          className="rounded-md border border-border px-3 py-1 text-xs disabled:cursor-not-allowed disabled:opacity-50"
        >
          Ignore
        </button>
      </div>
    </article>
  )
}
