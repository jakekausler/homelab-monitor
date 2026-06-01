import { HighlightStyle, StreamLanguage, syntaxHighlighting } from '@codemirror/language'
import type { Extension } from '@codemirror/state'
import { tags } from '@lezer/highlight'

// LogsQL keywords (pipe stages + boolean operators). Case-insensitive match on
// the lowercased word. Basic highlighting only — NOT a full grammar.
const KEYWORDS = new Set<string>([
  'and',
  'or',
  'not',
  'stats',
  'count',
  'count_uniq',
  'sort',
  'head',
  'limit',
  'fields',
  'filter',
  'by',
  'as',
])

// Well-known stream/field names for the homelab log schema. Highlighted as
// property names so a query reads field:value at a glance.
const FIELD_NAMES = new Set<string>([
  '_msg',
  '_time',
  '_stream',
  '_stream_id',
  'service',
  'host',
  'severity',
  'compose_service',
  'compose_project',
  'container_name',
  'cron_fingerprint',
  'image',
])

// State is unused (no multi-line constructs) but StreamLanguage requires a
// startState; return an empty object.
interface LogsQlState {
  readonly _: 0
}

export const logsQlStreamLanguage = StreamLanguage.define<LogsQlState>({
  startState(): LogsQlState {
    return { _: 0 }
  },
  token(stream): string | null {
    // 1. Whitespace — consume and emit no token.
    if (stream.eatSpace()) {
      return null
    }

    const ch = stream.peek()
    if (ch === null || ch === undefined) {
      // Defensive: peek at EOL. Advance to guarantee progress, emit nothing.
      stream.next()
      return null
    }

    // 2. Strings: double- or single-quoted with backslash escapes.
    if (ch === '"' || ch === "'") {
      const quote = ch
      stream.next() // opening quote
      let escaped = false
      while (!stream.eol()) {
        const c = stream.next()
        if (escaped) {
          escaped = false
          continue
        }
        if (c === '\\') {
          escaped = true
          continue
        }
        if (c === quote) {
          break
        }
      }
      return 'string'
    }

    // 3. Multi-char operators: :>=  :<=  :>  :<  (check longest first).
    if (
      stream.match(':>=', true) ||
      stream.match(':<=', true) ||
      stream.match(':>', true) ||
      stream.match(':<', true)
    ) {
      return 'operator'
    }

    // 4. Single-char operators: | : = -
    if (ch === '|' || ch === ':' || ch === '=' || ch === '-') {
      stream.next()
      return 'operator'
    }

    // 5. Numbers + duration literals: \d+ optionally followed by s/m/h/d.
    if (ch >= '0' && ch <= '9') {
      stream.match(/^\d+(?:\.\d+)?[smhd]?/)
      return 'number'
    }

    // 6. Words: [A-Za-z_][A-Za-z0-9_]* → keyword | field | identifier.
    const word = stream.match(/^[A-Za-z_][A-Za-z0-9_]*/)
    if (word && typeof word !== 'boolean') {
      const text = word[0]
      if (KEYWORDS.has(text.toLowerCase())) {
        return 'keyword'
      }
      if (FIELD_NAMES.has(text)) {
        return 'propertyName'
      }
      return null
    }

    // 7. Fallback: any other single char (e.g. punctuation) — ALWAYS advance.
    stream.next()
    return null
  },
  tokenTable: {
    // StreamLanguage's default tokenTable already maps keyword/string/number/
    // operator; only propertyName needs an explicit mapping.
    propertyName: tags.propertyName,
  },
})

// Theme-consistent colors. Sane hex that reads on both light and dark surfaces;
// the editor background follows the page (transparent theme in Impl). Kept
// simple per D-SYNTAX-HIGHLIGHTING-V1.
export const logsQlHighlightStyle = HighlightStyle.define([
  { tag: tags.keyword, color: '#7c3aed', fontWeight: '600' }, // violet-600
  { tag: tags.propertyName, color: '#2563eb' }, // blue-600
  { tag: tags.string, color: '#16a34a' }, // green-600
  { tag: tags.number, color: '#ea580c' }, // orange-600
  { tag: tags.operator, color: '#6b7280' }, // gray-500
])

/**
 * The LogsQL editor extensions: the StreamLanguage + its highlight style.
 * Imported by LogsQlEditorImpl (the lazy target).
 */
export function logsQlExtensions(): Extension[] {
  return [logsQlStreamLanguage, syntaxHighlighting(logsQlHighlightStyle)]
}
