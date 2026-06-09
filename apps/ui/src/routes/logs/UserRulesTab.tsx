import { useMemo, useState, type JSX } from 'react'

import { Button } from '@/components/ui/button'
import { EmptyState } from '@/components/EmptyState'
import { CreateAlertModal, type CreateAlertFormValues } from '@/components/logs/CreateAlertModal'
import {
  useUserRules,
  useUserRulesHealth,
  useEnableUserRule,
  useDisableUserRule,
  useDeleteUserRule,
  type LogUserRuleResponse,
} from '@/api/userRules'

type Health = 'ok' | 'err' | 'unknown'

function HealthBadge({ health, lastError }: { health: Health; lastError: string }): JSX.Element {
  const cls =
    health === 'ok'
      ? 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-100'
      : health === 'err'
        ? 'bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-100'
        : 'bg-muted text-muted-foreground'
  return (
    <span
      data-testid="user-rule-health"
      data-health={health}
      title={health === 'err' ? lastError : undefined}
      className={`inline-block rounded px-2 py-1 text-xs font-medium ${cls}`}
    >
      {health}
    </span>
  )
}

export function UserRulesTab(): JSX.Element {
  const { data, isLoading, error } = useUserRules()
  const healthQuery = useUserRulesHealth()
  const enableMut = useEnableUserRule()
  const disableMut = useDisableUserRule()
  const deleteMut = useDeleteUserRule()

  const [editRuleId, setEditRuleId] = useState<number | null>(null)
  const [editOpen, setEditOpen] = useState(false)

  const healthMap = useMemo(() => healthQuery.data?.rules ?? {}, [healthQuery.data])
  const rules: LogUserRuleResponse[] = data?.rules ?? []

  function healthFor(name: string): { health: Health; lastError: string } {
    const h = healthMap[name]
    if (!h) return { health: 'unknown', lastError: '' }
    return { health: h.health, lastError: h.last_error }
  }

  // Edit launches the modal in Advanced mode: we cannot reliably reverse-engineer
  // Simple-mode structured fields from an already-compiled LogsQL expr.
  function openEdit(rule: LogUserRuleResponse): void {
    setEditRuleId(rule.id)
    setEditOpen(true)
  }

  const editingRule = rules.find((r) => r.id === editRuleId) ?? null
  const editInitial: Partial<CreateAlertFormValues> | undefined = editingRule
    ? {
        rule_name: editingRule.rule_name,
        expr: editingRule.expr,
        expr_kind: editingRule.expr_kind,
        severity: editingRule.severity,
        summary: editingRule.summary,
        description: editingRule.description,
        for_duration: editingRule.for_duration,
      }
    : undefined

  function confirmDelete(rule: LogUserRuleResponse): void {
    if (window.confirm(`Delete alert rule "${rule.rule_name}"? This cannot be undone.`)) {
      deleteMut.mutate(rule.id)
    }
  }

  if (error !== null) {
    return <div className="p-4 text-sm text-destructive">Error loading alert rules</div>
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="min-h-0 flex-1 space-y-6 overflow-auto p-4">
        <div data-testid="user-rules-description" className="min-w-0">
          <h1 className="text-2xl font-semibold tracking-tight">Alert Rules</h1>
          <p className="text-sm text-muted-foreground">
            User-authored vmalert rules. Health reflects whether vmalert has loaded each rule.
          </p>
        </div>

        <div className="min-w-0 rounded-lg border border-border bg-card p-4">
          {isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}

          {rules.length === 0 && !isLoading && (
            <EmptyState testId="user-rules-empty">No alert rules yet.</EmptyState>
          )}

          {rules.length > 0 && (
            <>
              {/* Desktop table */}
              <div className="hidden md:block overflow-x-auto">
                <table data-testid="user-rules-table" className="w-full text-sm border-collapse">
                  <thead>
                    <tr className="border-b border-border">
                      <th className="px-4 py-2 text-left font-medium">Name</th>
                      <th className="px-4 py-2 text-left font-medium">Kind</th>
                      <th className="px-4 py-2 text-left font-medium">Severity</th>
                      <th className="px-4 py-2 text-left font-medium">Enabled</th>
                      <th className="px-4 py-2 text-left font-medium">Health</th>
                      <th className="px-4 py-2 text-left font-medium">Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rules.map((r) => {
                      const { health, lastError } = healthFor(r.rule_name)
                      return (
                        <tr
                          key={r.id}
                          data-testid="user-rules-row"
                          data-rule-id={r.id}
                          className="border-b border-border hover:bg-muted/50"
                        >
                          <td className="px-4 py-2 font-medium">{r.rule_name}</td>
                          <td className="px-4 py-2">{r.expr_kind}</td>
                          <td className="px-4 py-2">{r.severity}</td>
                          <td className="px-4 py-2">{r.enabled ? 'on' : 'off'}</td>
                          <td className="px-4 py-2">
                            <HealthBadge health={health} lastError={lastError} />
                          </td>
                          <td className="px-4 py-2">
                            <div className="flex gap-2">
                              <Button
                                type="button"
                                variant="ghost"
                                size="sm"
                                data-testid="user-rule-toggle"
                                disabled={enableMut.isPending || disableMut.isPending}
                                onClick={() =>
                                  r.enabled ? disableMut.mutate(r.id) : enableMut.mutate(r.id)
                                }
                              >
                                {r.enabled ? 'Disable' : 'Enable'}
                              </Button>
                              <Button
                                type="button"
                                variant="ghost"
                                size="sm"
                                data-testid="user-rule-edit"
                                onClick={() => openEdit(r)}
                              >
                                Edit
                              </Button>
                              <Button
                                type="button"
                                variant="ghost"
                                size="sm"
                                data-testid="user-rule-delete"
                                disabled={deleteMut.isPending}
                                onClick={() => confirmDelete(r)}
                              >
                                Delete
                              </Button>
                            </div>
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>

              {/* Mobile cards */}
              <ul data-testid="user-rules-cards" className="md:hidden space-y-2">
                {rules.map((r) => {
                  const { health, lastError } = healthFor(r.rule_name)
                  return (
                    <li
                      key={r.id}
                      data-testid="user-rules-row"
                      data-rule-id={r.id}
                      className="min-w-0 rounded border border-border bg-muted/50 p-3 space-y-2"
                    >
                      <div className="flex items-center justify-between gap-2">
                        <div className="min-w-0 truncate font-medium text-sm">{r.rule_name}</div>
                        <HealthBadge health={health} lastError={lastError} />
                      </div>
                      <div className="text-xs text-muted-foreground">
                        {r.expr_kind} · {r.severity} · {r.enabled ? 'enabled' : 'disabled'}
                      </div>
                      <div className="flex gap-2">
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          data-testid="user-rule-toggle"
                          disabled={enableMut.isPending || disableMut.isPending}
                          onClick={() =>
                            r.enabled ? disableMut.mutate(r.id) : enableMut.mutate(r.id)
                          }
                        >
                          {r.enabled ? 'Disable' : 'Enable'}
                        </Button>
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          data-testid="user-rule-edit"
                          onClick={() => openEdit(r)}
                        >
                          Edit
                        </Button>
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          data-testid="user-rule-delete"
                          disabled={deleteMut.isPending}
                          onClick={() => confirmDelete(r)}
                        >
                          Delete
                        </Button>
                      </div>
                    </li>
                  )
                })}
              </ul>
            </>
          )}
        </div>
      </div>

      {editRuleId !== null && (
        <CreateAlertModal
          open={editOpen}
          onOpenChange={(next) => {
            setEditOpen(next)
            if (!next) setEditRuleId(null)
          }}
          editRuleId={editRuleId}
          {...(editInitial !== undefined ? { initialValues: editInitial } : {})}
          initialMode="advanced"
        />
      )}
    </div>
  )
}
