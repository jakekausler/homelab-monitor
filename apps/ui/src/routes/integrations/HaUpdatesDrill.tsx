import { useHomeAssistantUpdates } from '@/api/home_assistant'

import { DrillList } from './DrillList'
import type { HaUpdateRow } from './types'

function renderRow(row: HaUpdateRow) {
  const label = row.title.trim() === '' ? row.entity_id : row.title
  const installed = row.installed_version
  const latest = row.latest_version
  const versionText = installed && latest ? `${installed} → ${latest}` : (latest ?? installed ?? '')
  const showSecondary = versionText !== '' || row.release_url != null
  return (
    <div className="space-y-0.5">
      <div className="flex items-baseline justify-between gap-2">
        <span className="min-w-0 truncate font-medium">{label}</span>
        <span className="shrink-0 text-xs text-muted-foreground">{row.entity_id}</span>
      </div>
      {showSecondary && (
        <p className="flex flex-wrap items-baseline gap-2 text-xs text-muted-foreground">
          {versionText !== '' && <span className="tabular-nums">{versionText}</span>}
          {row.release_url != null && (
            <a
              href={row.release_url}
              target="_blank"
              rel="noopener noreferrer"
              className="shrink-0 hover:text-foreground"
            >
              Release notes ↗
            </a>
          )}
        </p>
      )}
    </div>
  )
}

export function HaUpdatesDrill() {
  const result = useHomeAssistantUpdates()
  return (
    <DrillList<HaUpdateRow>
      items={result.data?.updates ?? []}
      isPending={result.isPending}
      error={result.error}
      renderRow={renderRow}
      emptyLabel="No updates pending"
      total={result.data?.total ?? 0}
      returned={result.data?.returned ?? 0}
      keyExtractor={(row) => row.entity_id}
    />
  )
}
