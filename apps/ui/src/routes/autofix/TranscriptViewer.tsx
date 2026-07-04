import type { JSX } from 'react'
import type { Transcript } from '@/api/autofix-runs'

interface Props {
  transcript: Transcript
}

export function TranscriptViewer({ transcript }: Props): JSX.Element {
  return (
    <div className="space-y-2">
      {transcript.truncated && (
        <div
          className="rounded border border-yellow-500 bg-yellow-50 px-3 py-2 text-sm text-yellow-900"
          role="status"
          data-testid="transcript-truncated-banner"
        >
          Transcript truncated to last 512 KB (original: {transcript.size_bytes.toLocaleString()}{' '}
          bytes).
        </div>
      )}
      <pre
        className="max-h-[600px] overflow-x-auto overflow-y-auto whitespace-pre-wrap
                   rounded-md bg-muted p-4 font-mono text-xs"
        data-testid="transcript-pre"
      >
        {transcript.text}
      </pre>
    </div>
  )
}
