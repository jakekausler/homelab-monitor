/**
 * Minimal ANSI SGR (Select Graphic Rendition) parser for log rendering.
 *
 * Tokenizes CSI SGR sequences (`\x1b[ ... m`) and emits a flat list of text
 * segments, each carrying the Tailwind classes active for that run of text.
 *
 * Scope (YAGNI — STAGE-004-003): 16-color fg/bg, bold, dim, underline, and the
 * matching resets. 256-color (`38;5;n`) and truecolor (`38;2;r;g;b`) param runs
 * are CONSUMED AND IGNORED so they never leak into the rendered text; we do not
 * render those colors. Unknown codes are ignored.
 *
 * Tailwind v4 note: every class below is a COMPLETE literal string in a Record.
 * Never build these by interpolation — the build scanner would drop them.
 */

export interface AnsiSegment {
  text: string
  classes: string
}

interface SgrState {
  fg: string
  bg: string
  bold: boolean
  dim: boolean
  underline: boolean
}

const FG: Record<number, string> = {
  30: 'text-neutral-500',
  31: 'text-red-500',
  32: 'text-green-500',
  33: 'text-yellow-500',
  34: 'text-blue-500',
  35: 'text-fuchsia-500',
  36: 'text-cyan-500',
  37: 'text-neutral-300',
  90: 'text-neutral-400',
  91: 'text-red-400',
  92: 'text-green-400',
  93: 'text-yellow-400',
  94: 'text-blue-400',
  95: 'text-fuchsia-400',
  96: 'text-cyan-400',
  97: 'text-white',
}

const BG: Record<number, string> = {
  40: 'bg-neutral-500/30',
  41: 'bg-red-500/30',
  42: 'bg-green-500/30',
  43: 'bg-yellow-500/30',
  44: 'bg-blue-500/30',
  45: 'bg-fuchsia-500/30',
  46: 'bg-cyan-500/30',
  47: 'bg-neutral-300/30',
}

// ANSI SGR sequences begin with the ESC control byte; matching it is intentional.
// eslint-disable-next-line no-control-regex
const SGR_RE = /\x1b\[([0-9;]*)m/g

function emptyState(): SgrState {
  return { fg: '', bg: '', bold: false, dim: false, underline: false }
}

/** Join the active classes for a state into a space-separated string ('' when none). */
export function classesFor(state: SgrState): string {
  const parts: string[] = []
  if (state.fg !== '') parts.push(state.fg)
  if (state.bg !== '') parts.push(state.bg)
  if (state.bold) parts.push('font-bold')
  if (state.dim) parts.push('opacity-70')
  if (state.underline) parts.push('underline')
  return parts.join(' ')
}

/**
 * Apply one SGR parameter list (the numbers between `\x1b[` and `m`) to `state`.
 * Mutates `state` in place. Empty params (`\x1b[m`) is treated as a `0` reset.
 */
function applyParams(params: number[], state: SgrState): void {
  if (params.length === 0) {
    resetAll(state)
    return
  }
  for (let i = 0; i < params.length; i++) {
    const code = params[i] ?? 0
    if (code === 0) {
      resetAll(state)
    } else if (code === 1) {
      state.bold = true
    } else if (code === 2) {
      state.dim = true
    } else if (code === 4) {
      state.underline = true
    } else if (code === 22) {
      state.bold = false
      state.dim = false
    } else if (code === 24) {
      state.underline = false
    } else if (code === 39) {
      state.fg = ''
    } else if (code === 49) {
      state.bg = ''
    } else if (code >= 30 && code <= 37) {
      state.fg = FG[code] ?? ''
    } else if (code >= 40 && code <= 47) {
      state.bg = BG[code] ?? ''
    } else if (code >= 90 && code <= 97) {
      state.fg = FG[code] ?? ''
    } else if (code === 38 || code === 48) {
      // Extended color: consume-and-ignore the following params so they don't
      // leak. `38;5;n` / `48;5;n` = 256-color (1 extra param after the 5).
      // `38;2;r;g;b` / `48;2;r;g;b` = truecolor (3 extra params after the 2).
      const target = params[i + 1]
      if (target === 5) {
        i += 2 // skip the `5` and the single color index
      } else if (target === 2) {
        i += 4 // skip the `2` and the r,g,b triplet
      } else {
        i += 1 // malformed; skip the mode byte and bail to next iteration
      }
    }
    // 23 and any unknown code: intentionally ignored.
  }
}

function resetAll(state: SgrState): void {
  state.fg = ''
  state.bg = ''
  state.bold = false
  state.dim = false
  state.underline = false
}

/**
 * Parse a raw log line containing ANSI SGR escapes into styled segments.
 * Fast path: a line with no escapes returns a single plain segment.
 */
export function parseAnsi(raw: string): AnsiSegment[] {
  if (!raw.includes('\x1b[')) return [{ text: raw, classes: '' }]

  const segments: AnsiSegment[] = []
  const state = emptyState()
  let lastIndex = 0
  SGR_RE.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = SGR_RE.exec(raw)) !== null) {
    const text = raw.slice(lastIndex, m.index)
    if (text !== '') segments.push({ text, classes: classesFor(state) })
    const paramStr = m[1] ?? ''
    const params = paramStr === '' ? [] : paramStr.split(';').map((p) => Number(p))
    applyParams(params, state)
    lastIndex = SGR_RE.lastIndex
  }
  // Trailing text after the last escape (covers unterminated-at-EOL too:
  // an unterminated `\x1b[` with no closing `m` simply won't match, so the
  // whole remainder including the stray bytes renders as one plain segment).
  const tail = raw.slice(lastIndex)
  if (tail !== '') segments.push({ text: tail, classes: classesFor(state) })
  if (segments.length === 0) segments.push({ text: '', classes: '' })
  return segments
}
