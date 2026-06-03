// Pure helpers + capped node model for the Field Inspector JSON message tree.
//
// This module MUST stay free of React / lucide-react / @codemirror imports so it
// can be unit-tested without pulling a component or heavy chunk into the test's
// module graph (same convention as logsQlEditorUtils.ts). JsonMessageTree imports
// detectJsonMessage + buildJsonNode from here. STAGE-004-016B.

/** Caps for the recursive render model. */
export const JSON_MAX_DEPTH = 10
export const JSON_MAX_CHILDREN = 1000
export const JSON_MAX_NODES = 5000

/** Result of inspecting a log line's `message`. */
export type JsonMessageDetection = { kind: 'tree'; value: unknown } | { kind: 'text' }

/**
 * Detect whether `message` is JSON that should render as a tree.
 * Tree IFF JSON.parse succeeds AND the parsed result is a non-null object
 * (objects AND arrays, including empty {} and []). Bare primitives
 * (numbers, quoted strings, booleans, null) and parse failures → text.
 */
export function detectJsonMessage(message: string): JsonMessageDetection {
  let parsed: unknown
  try {
    parsed = JSON.parse(message)
  } catch {
    return { kind: 'text' }
  }
  if (typeof parsed === 'object' && parsed !== null) {
    return { kind: 'tree', value: parsed }
  }
  return { kind: 'text' }
}

/**
 * Top-level keys of a JSON object, used to suppress duplicate flattened bag
 * rows. Returns Object.keys for a non-array object; [] for arrays, primitives,
 * and null.
 */
export function jsonTopLevelKeys(value: unknown): string[] {
  if (typeof value === 'object' && value !== null && !Array.isArray(value)) {
    return Object.keys(value)
  }
  return []
}

// ---------------------------------------------------------------------------
// Capped render model (pure pre-pass).
// ---------------------------------------------------------------------------

/** A primitive JSON leaf (string | number | boolean | null). */
export interface JsonLeafNode {
  type: 'leaf'
  /** Stable path, e.g. 'root', 'root.collector', 'root[0]'. */
  path: string
  /** The object key or null for array elements / the root. */
  label: string | null
  /** Discriminates leaf rendering style. */
  valueKind: 'string' | 'number' | 'boolean' | 'null'
  /** Display text. Strings are quoted; numbers/booleans/null stringified. */
  text: string
}

/** A branch: object or array with children. */
export interface JsonBranchNode {
  type: 'branch'
  path: string
  label: string | null
  /** 'object' → {…}, 'array' → […]. */
  container: 'object' | 'array'
  /** Total child count BEFORE the child cap (for the "(M more)" indicator). */
  childCount: number
  children: JsonNode[]
  /** Number of children hidden by the 1000-child cap (0 when none). */
  hiddenCount: number
}

/** A subtree replaced by a single collapsed leaf because a cap was hit. */
export interface JsonTruncatedNode {
  type: 'truncated'
  path: string
  label: string | null
  reason: 'depth' | 'budget'
  /** Compact JSON.stringify of the elided value (single line). */
  preview: string
}

export type JsonNode = JsonLeafNode | JsonBranchNode | JsonTruncatedNode

/** Union type for leaf value kinds. */
export type JsonLeafKind = JsonLeafNode['valueKind']

interface Budget {
  remaining: number
}

function leafKind(value: string | number | boolean | null): JsonLeafKind {
  if (value === null) return 'null'
  if (typeof value === 'string') return 'string'
  if (typeof value === 'number') return 'number'
  return 'boolean'
}

function leafText(value: string | number | boolean | null): string {
  if (value === null) return 'null'
  if (typeof value === 'string') return JSON.stringify(value) // quoted
  return String(value)
}

/**
 * Build the capped render model for a parsed JSON value.
 * `depth` starts at 0 for the root. Caller passes the parsed value from
 * detectJsonMessage; this function creates its own budget.
 */
export function buildJsonModel(value: unknown): JsonNode {
  const budget: Budget = { remaining: JSON_MAX_NODES }
  return buildJsonNode(value, 'root', null, 0, budget)
}

function buildJsonNode(
  value: unknown,
  path: string,
  label: string | null,
  depth: number,
  budget: Budget,
): JsonNode {
  // Each emitted node costs one unit. Charge before descending.
  budget.remaining -= 1

  // Budget exhausted → collapse remaining subtree to a raw-string leaf.
  if (budget.remaining < 0) {
    return { type: 'truncated', path, label, reason: 'budget', preview: JSON.stringify(value) }
  }

  // Depth cap → collapse to a single compact leaf. Renders up to JSON_MAX_DEPTH (10) levels; deeper nodes truncated.
  if (depth >= JSON_MAX_DEPTH) {
    return { type: 'truncated', path, label, reason: 'depth', preview: JSON.stringify(value) }
  }

  if (Array.isArray(value)) {
    const total = value.length
    const visible = Math.min(total, JSON_MAX_CHILDREN)
    const children: JsonNode[] = []
    for (let i = 0; i < visible; i++) {
      children.push(buildJsonNode(value[i], `${path}[${i}]`, String(i), depth + 1, budget))
    }
    return {
      type: 'branch',
      path,
      label,
      container: 'array',
      childCount: total,
      children,
      hiddenCount: total - visible,
    }
  }

  if (typeof value === 'object' && value !== null) {
    const obj = value as Record<string, unknown>
    const keys = Object.keys(obj)
    const total = keys.length
    const visible = Math.min(total, JSON_MAX_CHILDREN)
    const children: JsonNode[] = []
    for (let i = 0; i < visible; i++) {
      const key = keys[i] as string // i < visible <= keys.length → defined
      children.push(
        buildJsonNode(obj[key], `${path}.${encodeURIComponent(key)}`, key, depth + 1, budget),
      )
    }
    return {
      type: 'branch',
      path,
      label,
      container: 'object',
      childCount: total,
      children,
      hiddenCount: total - visible,
    }
  }

  // Primitive leaf. value is string | number | boolean | null here.
  const leaf = value as string | number | boolean | null
  return { type: 'leaf', path, label, valueKind: leafKind(leaf), text: leafText(leaf) }
}
