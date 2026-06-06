import { useCallback, useState } from 'react'
import type { JSX } from 'react'

import { useSignatureAnnotations, useAddAnnotation, useDeleteAnnotation } from '@/api/annotations'
import { Button } from '@/components/ui/button'
import { formatRelative } from '@/lib/relativeTime'

interface SignatureAnnotationsProps {
  templateHash: string
  serviceKey: string
}

export function SignatureAnnotations({
  templateHash,
  serviceKey,
}: SignatureAnnotationsProps): JSX.Element {
  const { data } = useSignatureAnnotations(templateHash, serviceKey)
  const addMut = useAddAnnotation()
  const deleteMut = useDeleteAnnotation()
  const [noteText, setNoteText] = useState('')

  const annotations = data?.annotations ?? []
  const canAdd = noteText.trim().length > 0 && !addMut.isPending

  const handleAdd = useCallback(() => {
    if (noteText.trim().length === 0) return
    addMut.mutate(
      { templateHash, serviceKey, body: { note: noteText } },
      { onSuccess: () => setNoteText('') },
    )
  }, [templateHash, serviceKey, noteText, addMut])

  const handleDelete = useCallback(
    (annotationId: number) => {
      if (!window.confirm('Delete this annotation?')) return
      deleteMut.mutate({ templateHash, serviceKey, annotationId })
    },
    [templateHash, serviceKey, deleteMut],
  )

  return (
    <div data-testid="signature-annotations">
      <h3 className="mb-2 text-sm font-semibold">Annotations</h3>

      {annotations.length === 0 ? (
        <div className="text-xs text-muted-foreground">No annotations yet.</div>
      ) : (
        <ul className="space-y-2">
          {annotations.map((a) => (
            <li
              key={a.id}
              data-testid="annotation-item"
              className="flex items-start justify-between gap-2 rounded-md border border-border bg-muted/30 p-2"
            >
              <div className="min-w-0">
                <div className="text-xs text-muted-foreground">
                  {a.author} · {formatRelative(a.created_at)}
                </div>
                <div className="whitespace-pre-wrap break-words text-sm">{a.note}</div>
              </div>
              <button
                type="button"
                aria-label="Delete annotation"
                onClick={() => handleDelete(a.id)}
                disabled={deleteMut.isPending}
                className="shrink-0 text-muted-foreground hover:text-foreground disabled:opacity-50"
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      )}

      <div className="mt-2 flex flex-col gap-2">
        <textarea
          value={noteText}
          maxLength={2000}
          onChange={(e) => setNoteText(e.currentTarget.value)}
          placeholder="Add a note..."
          data-testid="annotation-input"
          className="min-h-[60px] rounded-md border border-border bg-background px-2 py-1 text-sm"
          disabled={addMut.isPending}
        />
        <div>
          <Button
            size="sm"
            onClick={handleAdd}
            disabled={!canAdd}
            data-testid="annotation-add-button"
          >
            Add
          </Button>
        </div>
      </div>
    </div>
  )
}
