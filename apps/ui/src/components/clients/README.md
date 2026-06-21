# `components/clients` — Unifi client detail primitives

Shared building blocks for the Unifi client detail page (STAGE-007-022) and the
forward DNS-enrichment contract for EPIC-006 (Pi-hole).

## `ClientDnsSection`

Renders the DNS-activity slot on `/integrations/network/clients/{mac}`.

**Prop:** `dns: DnsEnrichment | null` — sourced from `UnifiClientDetail.dns`
(`apps/ui/src/api/schema.ts`, type `Schema<'DnsEnrichment'>`).

### Stable contract (EPIC-006)

Today the backend always returns `dns: null` (the slot is reserved). EPIC-006
(Pi-hole integration) will populate it with NO UI rework required.

- **Null branch (current):** honest placeholder PanelSection "DNS activity" with
  muted copy "DNS insights provided by Pi-hole — available in a future update."
  It MUST NOT render fake 0-counts or fake domains.
- **Populated branch (already implemented against the typed contract):**
  - `top_domains: string[]` — rendered as a list (`@default []`).
  - `blocked_count: number | null` — rendered as a number; `null` → "—".
  - `last_query_at: string | null` — ISO UTC, rendered via `formatRelative`;
    `null` → "—".

When EPIC-006 starts emitting a non-null `dns` object, this component lights up
automatically.

### Clients-tab DNS column hook

The Clients table (`apps/ui/src/routes/integrations/ClientsTable.tsx`) intentionally
carries NO DNS column today — `UnifiClientRowModel` does not include DNS fields.
The documented insertion point is the column-header / cell list in `ClientsTable`
(see the `// DNS column hook (EPIC-006)` comment). When EPIC-006 adds a
row-level DNS summary to the row model, add the column there.

Cross-reference: EPIC-006 (Pi-hole / DNS enrichment).
