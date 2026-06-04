// STAGE-004-021 — pure URL builder for deep-linking into the /logs Explorer.
// Mirrors LogsExplorerPage.writeUrl serialization (apps/ui/src/routes/logs/
// LogsExplorerPage.tsx:106-139) so a generated link round-trips through the
// route's validateSearch (apps/ui/src/router.tsx:139-163). PURE: no router, no
// DOM. 100%-tested (see __tests__/explorerLink.test.ts).

import { toIsoZ, type PresetToken } from '@/lib/timeRange'

/**
 * The URL search-param keys this builder can emit. A test asserts this is a
 * SUBSET of the keys the /logs route accepts (router.tsx validateSearch:
 * q, logsql, since, start, end, services).
 */
export const EXPLORER_URL_KEYS = ['logsql', 'q', 'since', 'start', 'end', 'services'] as const

export interface BuildExplorerUrlOptions {
  /** Ready-made LogsQL expression (advanced mode). Caller builds + quotes it. */
  logsQl?: string | undefined
  /** Plain-text search term (plain mode). Ignored if logsQl is non-empty. */
  plainText?: string | undefined
  /** Pre-formatted `<source_type>:<service>` strings. Joined with ','. */
  selectedServices?: string[] | undefined
  /** Preset window token. Takes precedence over rangeStart/rangeEnd. */
  sincePreset?: PresetToken | undefined
  /** Custom range start. Used only when sincePreset is absent. */
  rangeStart?: Date | undefined
  /** Custom range end. Used only when sincePreset is absent. */
  rangeEnd?: Date | undefined
}

/**
 * Build a `/logs?...` deep-link path.
 *
 * Precedence rules (documented + tested):
 *  - Query: if `logsQl` is a non-empty string → `logsql=<logsQl>` (logsQl WINS
 *    over plainText). Else if `plainText` is non-empty → `q=<plainText>`.
 *    Else neither.
 *  - Time: if `sincePreset` is provided → `since=<token>` (preset WINS over
 *    range). Else: if `rangeStart` provided → `start=<toIsoZ(rangeStart)>`; if
 *    `rangeEnd` provided → `end=<toIsoZ(rangeEnd)>`. `start` without `end` is
 *    allowed (open-ended).
 *  - Services: if `selectedServices` is non-empty → `services=<csv>`.
 *
 * Returns `/logs` (no `?`) when no params are emitted; otherwise
 * `/logs?<encoded>`. URLSearchParams handles percent-encoding.
 */
export function buildExplorerUrl(opts: BuildExplorerUrlOptions): string {
  const params = new URLSearchParams()

  if (opts.logsQl !== undefined && opts.logsQl.length > 0) {
    params.set('logsql', opts.logsQl)
  } else if (opts.plainText !== undefined && opts.plainText.length > 0) {
    params.set('q', opts.plainText)
  }

  if (opts.sincePreset !== undefined) {
    params.set('since', opts.sincePreset)
  } else {
    if (opts.rangeStart !== undefined) {
      params.set('start', toIsoZ(opts.rangeStart))
    }
    if (opts.rangeEnd !== undefined) {
      params.set('end', toIsoZ(opts.rangeEnd))
    }
  }

  if (opts.selectedServices !== undefined && opts.selectedServices.length > 0) {
    params.set('services', opts.selectedServices.join(','))
  }

  const query = params.toString()
  return query.length > 0 ? `/logs?${query}` : '/logs'
}
