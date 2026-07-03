import { Copy, Info, Play } from 'lucide-react'
import { useState } from 'react'
import type { JSX } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { cn } from '@/lib/utils'
import { useToggleRunbook, type Runbook } from '@/api/runbooks'
import { RunFixDialog } from './RunFixDialog'

interface RunbookCardProps {
  runbook: Runbook
  killSwitchEnabled: boolean
}

function basename(path: string): string {
  const idx = path.lastIndexOf('/')
  return idx === -1 ? path : path.slice(idx + 1)
}

function formatPattern(p: Record<string, unknown>): string {
  // Prefer high-value keys if present; otherwise compact JSON.
  const alertname = p['alertname']
  if (typeof alertname === 'string') return alertname
  const match = p['match']
  if (typeof match === 'string') return match
  try {
    return JSON.stringify(p)
  } catch {
    return '[pattern]'
  }
}

function RiskBadge({ tag }: { tag: string }): JSX.Element {
  if (tag === 'risky') {
    return (
      <span
        data-testid="runbook-risk-badge-risky"
        className="rounded px-1.5 py-0.5 text-xs font-medium bg-amber-500/10 text-amber-700 dark:text-amber-300"
      >
        risky
      </span>
    )
  }
  if (tag === 'safe') {
    return (
      <span
        data-testid="runbook-risk-badge-safe"
        className="rounded px-1.5 py-0.5 text-xs font-medium bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
      >
        safe
      </span>
    )
  }
  return (
    <span
      data-testid={`runbook-risk-badge-${tag}`}
      className="rounded px-1.5 py-0.5 text-xs font-medium bg-muted text-muted-foreground"
    >
      {tag}
    </span>
  )
}

function PatternChips({
  patterns,
  testId,
}: {
  patterns: Array<Record<string, unknown>>
  testId: string
}): JSX.Element | null {
  if (patterns.length === 0) return null
  const displayed = patterns.slice(0, 3)
  const remaining = patterns.length - displayed.length
  return (
    <div className="flex flex-wrap gap-1" data-testid={testId}>
      {displayed.map((p, i) => (
        <Badge key={i} variant="secondary" className="text-xs font-mono">
          {formatPattern(p)}
        </Badge>
      ))}
      {remaining > 0 && <span className="text-xs text-muted-foreground">+{remaining} more</span>}
    </div>
  )
}

function RateLimitSummary({
  rateLimit,
  cooldown,
}: {
  rateLimit: number | null
  cooldown: number | null
}): JSX.Element | null {
  if (rateLimit === null && cooldown === null) return null
  const parts: string[] = []
  if (rateLimit !== null) parts.push(`Rate limit: ${String(rateLimit)}/hr`)
  if (cooldown !== null) parts.push(`Cooldown: ${String(cooldown)}s`)
  return <p className="text-xs text-muted-foreground">{parts.join(' · ')}</p>
}

/** Native-button switch primitive: no external dep required. */
function ToggleSwitch({
  checked,
  disabled,
  onCheckedChange,
  ariaLabel,
  testId,
}: {
  checked: boolean
  disabled: boolean
  onCheckedChange: (v: boolean) => void
  ariaLabel: string
  testId: string
}): JSX.Element {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={ariaLabel}
      disabled={disabled}
      data-testid={testId}
      onClick={() => onCheckedChange(!checked)}
      className={cn(
        'relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
        disabled && 'cursor-not-allowed opacity-50',
        checked ? 'bg-primary' : 'bg-muted-foreground/30',
      )}
    >
      <span
        aria-hidden="true"
        className={cn(
          'inline-block h-4 w-4 rounded-full bg-white shadow transition-transform',
          checked ? 'translate-x-4' : 'translate-x-0.5',
        )}
      />
    </button>
  )
}

export function RunbookCard({ runbook, killSwitchEnabled }: RunbookCardProps): JSX.Element {
  const [runFixOpen, setRunFixOpen] = useState(false)
  const toggle = useToggleRunbook()

  const handleCopyPath = (): void => {
    if (typeof navigator !== 'undefined' && navigator.clipboard) {
      navigator.clipboard.writeText(runbook.path).catch(() => {
        /* silent — clipboard permissions vary */
      })
    }
  }

  return (
    <Card className="@container" data-testid={`runbook-card-${runbook.id}`}>
      <CardHeader>
        <div className="flex flex-col gap-2 @lg:flex-row @lg:items-start @lg:justify-between @lg:gap-4">
          <div className="min-w-0">
            <CardTitle className="text-base truncate">{basename(runbook.path)}</CardTitle>
            <CardDescription className="text-xs font-mono truncate">{runbook.path}</CardDescription>
          </div>
          <div className="flex flex-wrap items-center gap-1.5 @lg:shrink-0">
            <RiskBadge tag={runbook.risk_tag} />
            {runbook.dry_run_required && (
              <span
                data-testid="runbook-badge-dryrun"
                className="rounded px-1.5 py-0.5 text-xs font-medium bg-blue-500/10 text-blue-700 dark:text-blue-300"
              >
                dry-run required
              </span>
            )}
          </div>
        </div>
      </CardHeader>

      <CardContent className="space-y-4">
        <PatternChips
          patterns={runbook.alert_match_patterns}
          testId={`runbook-patterns-${runbook.id}`}
        />
        <RateLimitSummary
          rateLimit={runbook.rate_limit_per_hour ?? null}
          cooldown={runbook.cooldown_seconds ?? null}
        />

        <div className="space-y-3 border-t pt-3">
          <label className="flex items-center justify-between text-sm">
            <span className="font-medium">Enabled</span>
            <ToggleSwitch
              checked={runbook.enabled}
              disabled={!killSwitchEnabled || toggle.isPending}
              onCheckedChange={(checked) => toggle.mutate({ id: runbook.id, enabled: checked })}
              ariaLabel={`Toggle ${basename(runbook.path)} enabled`}
              testId={`runbook-toggle-enabled-${runbook.id}`}
            />
          </label>
          <label className="flex items-center justify-between text-sm">
            <span className="flex items-center gap-1.5 font-medium">
              Auto-trigger
              {runbook.risk_tag === 'risky' && (
                <span
                  title="Auto-trigger on a risky runbook produces a pending approval (not a real run). Approve to execute."
                  data-testid={`runbook-risky-info-${runbook.id}`}
                  aria-label="Risky runbook info"
                >
                  <Info className="h-3.5 w-3.5 text-muted-foreground" aria-hidden="true" />
                </span>
              )}
            </span>
            <ToggleSwitch
              checked={runbook.auto_trigger}
              disabled={!killSwitchEnabled || toggle.isPending}
              onCheckedChange={(checked) =>
                toggle.mutate({ id: runbook.id, auto_trigger: checked })
              }
              ariaLabel={`Toggle ${basename(runbook.path)} auto-trigger`}
              testId={`runbook-toggle-autotrigger-${runbook.id}`}
            />
          </label>
        </div>

        {/* Stats row — placeholders. Real values ship in STAGE-009-011. */}
        <div className="border-t pt-3 text-sm text-muted-foreground">
          <div className="flex items-center justify-between">
            <span>Last run</span>
            <span data-testid={`runbook-last-run-${runbook.id}`}>—</span>
          </div>
          <div className="mt-1 flex items-center justify-between">
            <span className="flex items-center gap-1.5">
              Success rate
              <span
                title="Runs history and per-runbook aggregation ship in Auto-fix history (STAGE-009-011)."
                aria-label="Success rate info"
              >
                <Info className="h-3.5 w-3.5" aria-hidden="true" />
              </span>
            </span>
            <span data-testid={`runbook-success-rate-${runbook.id}`}>—</span>
          </div>
        </div>
      </CardContent>

      <CardFooter className="text-xs text-muted-foreground">
        <div className="flex w-full flex-col gap-2 @md:flex-row @md:items-center @md:justify-between">
          <p className="truncate text-xs text-muted-foreground" title={runbook.path}>
            {runbook.path}
          </p>
          <div className="flex flex-col gap-2 @md:flex-row">
            <Button
              variant="ghost"
              size="sm"
              onClick={handleCopyPath}
              data-testid={`runbook-copy-path-${runbook.id}`}
              className="w-full @md:w-auto"
            >
              <Copy className="mr-1 h-3.5 w-3.5" aria-hidden="true" />
              <span className="sr-only">Copy config folder path</span>
              Copy config folder path
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setRunFixOpen(true)}
              disabled={!killSwitchEnabled || !runbook.enabled}
              data-testid={`runbook-run-fix-${runbook.id}`}
              title={
                !killSwitchEnabled
                  ? 'Kill switch is engaged'
                  : !runbook.enabled
                    ? 'Runbook is disabled'
                    : 'Manually trigger this runbook'
              }
              className="w-full @md:w-auto"
            >
              <Play className="mr-1 h-3.5 w-3.5" aria-hidden="true" />
              <span className="sr-only">Run fix</span>
              Run fix
            </Button>
          </div>
        </div>
      </CardFooter>
      <RunFixDialog runbook={runbook} open={runFixOpen} onOpenChange={setRunFixOpen} />
    </Card>
  )
}
