import { useCallback } from 'react'
import { toast } from 'sonner'

import { copyToClipboard } from './clipboard'

/**
 * Returns a `copy(text, label?)` callback that copies to the clipboard and
 * fires a sonner toast: success → "<label> copied" (or "Copied"), failure →
 * "Copy failed". STAGE-004-016.
 */
export function useCopyToClipboard(): (text: string, label?: string) => Promise<void> {
  return useCallback(async (text: string, label?: string) => {
    const ok = await copyToClipboard(text)
    if (ok) {
      toast.success(label !== undefined ? `${label} copied` : 'Copied')
    } else {
      toast.error('Copy failed')
    }
  }, [])
}
