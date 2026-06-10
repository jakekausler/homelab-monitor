import type {
  Completion,
  CompletionContext,
  CompletionResult,
  CompletionSource,
} from '@codemirror/autocomplete'
import type { MutableRefObject } from 'react'

import { FIELD_NAMES, KEYWORDS } from './logsQlLanguage'
import type { Schema } from '@/api/types'

/** The discovered-field shape the completion source consumes (subset of FieldDescriptor). */
export type FieldsForCompletion = readonly Schema<'FieldDescriptor'>[] | undefined

/**
 * Pure LogsQL completion source. Heuristic (NOT a full LogsQL parser):
 *  - `field:partial` → that field's sample_values (needs `fields`).
 *  - `| partial`     → KEYWORDS (pipe functions / operators).
 *  - bare `partial`  → FIELD_NAMES ∪ discovered field names, inserting `name:`.
 *
 * Returns a CompletionResult or null. Never throws. Reads NOTHING outside its args.
 */
export function logsQlCompletionSource(
  context: CompletionContext,
  fields: FieldsForCompletion,
): CompletionResult | null {
  // --- 1. Value context: `<field>:<partial>` immediately before the cursor. ---
  const valueMatch = context.matchBefore(/([A-Za-z_][\w.]*):(\S*)$/)
  if (valueMatch !== null) {
    const colonIdx = valueMatch.text.indexOf(':')
    const fieldName = valueMatch.text.slice(0, colonIdx)
    const partial = valueMatch.text.slice(colonIdx + 1)
    const descriptor = fields?.find((f) => f.name === fieldName)
    if (descriptor === undefined) return null
    const lower = partial.toLowerCase()
    const options: Completion[] = descriptor.sample_values
      .filter((v) => v.toLowerCase().includes(lower))
      .map((v) => ({ label: v, type: 'text' }))
    if (options.length === 0) return null
    // `from` is the start of the VALUE (just after the colon).
    return { from: valueMatch.from + colonIdx + 1, options }
  }

  // --- 2. Pipe/keyword context: `| <partial>` before the cursor. ---
  const pipeMatch = context.matchBefore(/\|\s*(\w*)$/)
  if (pipeMatch !== null) {
    const partial = (/\w*$/.exec(pipeMatch.text)?.[0] ?? '').toLowerCase()
    if (partial.length === 0 && !context.explicit) return null
    const options: Completion[] = [...KEYWORDS]
      .filter((k) => k.toLowerCase().startsWith(partial))
      .map((k) => ({ label: k, type: 'keyword', apply: `${k} ` }))
    if (options.length === 0) return null
    // `from` = start of the partial word (end-of-text minus partial length).
    return { from: pipeMatch.to - partial.length, options }
  }

  // --- 3. Field-name context: bare partial word (catch-all). ---
  const wordMatch = context.matchBefore(/[A-Za-z_][\w.]*$/)
  const partial = (wordMatch?.text ?? '').toLowerCase()
  if (partial.length === 0 && !context.explicit) return null
  const names = new Set<string>(FIELD_NAMES)
  for (const f of fields ?? []) names.add(f.name)
  const options: Completion[] = [...names]
    .filter((n) => n.toLowerCase().startsWith(partial))
    .sort((a, b) => a.localeCompare(b))
    .map((n) => ({ label: n, type: 'property', apply: `${n}:` }))
  if (options.length === 0) return null
  return { from: wordMatch?.from ?? context.pos, options }
}

/**
 * Build the CM6 CompletionSource closure. The closure reads `fieldsRef.current`
 * AT CALL TIME (every invocation) so the EditorView, created once, always sees
 * the latest discovered fields without being re-created.
 *
 * CRITICAL: do NOT capture `fieldsRef.current` by value here. Read it INSIDE the
 * returned function.
 */
export function makeLogsQlCompletionSource(
  fieldsRef: MutableRefObject<FieldsForCompletion>,
): CompletionSource {
  return (context: CompletionContext): CompletionResult | null =>
    logsQlCompletionSource(context, fieldsRef.current)
}
