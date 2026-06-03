/**
 * Copy text to the clipboard. Tries the async Clipboard API first, falls back
 * to a temp <textarea> + execCommand('copy') for non-secure contexts / older
 * browsers. Returns true on success, false on any failure. Never throws.
 */
export async function copyToClipboard(text: string): Promise<boolean> {
  // Primary path: async Clipboard API (requires a secure context).
  if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // fall through to the execCommand fallback
    }
  }
  // Fallback path: temp textarea + execCommand('copy').
  if (typeof document === 'undefined') return false
  try {
    const textarea = document.createElement('textarea')
    textarea.value = text
    // Keep it off-screen and non-disruptive.
    textarea.style.position = 'fixed'
    textarea.style.opacity = '0'
    textarea.style.pointerEvents = 'none'
    document.body.appendChild(textarea)
    textarea.focus()
    textarea.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(textarea)
    return ok
  } catch {
    return false
  }
}
