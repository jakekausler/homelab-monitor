import { useCallback, useEffect, useState } from 'react'

import { clearHistory, readHistory, subscribe, type HistoryEntry } from './queryHistory'

export function useQueryHistory(): { entries: HistoryEntry[]; clear: () => void } {
  const [entries, setEntries] = useState<HistoryEntry[]>(readHistory)

  useEffect(() => {
    // Re-read on any same-tab write (recordQuery/clearHistory emit).
    const unsubscribe = subscribe(() => {
      setEntries(readHistory())
    })
    return unsubscribe
  }, [])

  const clear = useCallback(() => {
    clearHistory() // emits → subscriber above re-reads → entries becomes []
  }, [])

  return { entries, clear }
}
