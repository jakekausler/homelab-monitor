import { useState } from 'react'
import type { JSX } from 'react'

import { useUnifiClients } from '@/api/unifi'

import { ClientsTable } from './ClientsTable'
import { PanelSection } from './PanelSection'
import { QueryState } from './QueryState'

export function NetworkClientsTab(): JSX.Element {
  const [search, setSearch] = useState('')
  // Simple debounce: a controlled input drives `search`; we apply it directly.
  // Filtering is a pure Array.filter (cheap for ~86 rows), so no timer is required.
  // NOTE: fetch-all caps at 500 clients (this homelab ~86). If client count
  // approaches 500, add a server-side sort/filter param. (STAGE-007-022 Design)
  const result = useUnifiClients(500, 0)

  return (
    <div className="h-full space-y-4 overflow-y-auto p-4">
      <PanelSection title="Clients">
        <div className="mb-3">
          <input
            type="search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search name, hostname, IP, or MAC…"
            className="w-full max-w-sm rounded-md border border-border bg-background px-3 py-1.5 text-sm"
            data-testid="clients-search"
            aria-label="Search clients"
          />
        </div>
        <QueryState
          result={result}
          unavailableLabel="Client inventory temporarily unavailable"
          renderData={(data) => <ClientsTable rows={data.clients} query={search} />}
        />
      </PanelSection>
    </div>
  )
}
