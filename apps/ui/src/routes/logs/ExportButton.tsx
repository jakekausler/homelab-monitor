import { useState } from 'react'
import { Download } from 'lucide-react'

import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogTitle,
} from '@/components/ui/dialog'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

const MIN_MAX = 1
const MAX_MAX = 100000
const DEFAULT_MAX = 10000

export type ExportFormat = 'txt' | 'json'

export interface BuildExportUrlOptions {
  expr: string
  startIso: string
  endIso: string
  format: ExportFormat
  max: number
  servicesCsv: string
}

/**
 * Build the absolute (same-origin) export URL. `servicesCsv` is omitted from the
 * query string when empty (exactOptionalPropertyTypes-friendly). `max` is clamped
 * to [1, 100000] to mirror the backend's accepted range.
 */
export function buildExportUrl(opts: BuildExportUrlOptions): string {
  const clampedMax = Math.min(MAX_MAX, Math.max(MIN_MAX, Math.trunc(opts.max)))
  const params = new URLSearchParams({
    expr: opts.expr,
    start: opts.startIso,
    end: opts.endIso,
    format: opts.format,
    max: String(clampedMax),
  })
  if (opts.servicesCsv.length > 0) {
    params.set('services', opts.servicesCsv)
  }
  return `/api/logs/export?${params.toString()}`
}

interface ExportButtonProps {
  expr: string
  startIso: string
  endIso: string
  servicesCsv: string
}

export function ExportButton({ expr, startIso, endIso, servicesCsv }: ExportButtonProps) {
  const [open, setOpen] = useState(false)
  const [format, setFormat] = useState<ExportFormat>('txt')
  const [maxLines, setMaxLines] = useState<number>(DEFAULT_MAX)

  const handleDownload = (): void => {
    const url = buildExportUrl({ expr, startIso, endIso, format, max: maxLines, servicesCsv })
    // Hidden-anchor download: same-origin GET carries the session cookie; the
    // server's Content-Disposition: attachment makes the browser save it.
    const a = document.createElement('a')
    a.href = url
    a.setAttribute('download', '')
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    setOpen(false)
  }

  return (
    <>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            type="button"
            size="sm"
            variant="outline"
            className="h-8 w-8 p-0"
            data-testid="logs-export-button"
            aria-label="Export logs"
            onClick={() => setOpen(true)}
          >
            <Download />
          </Button>
        </TooltipTrigger>
        <TooltipContent>Export logs</TooltipContent>
      </Tooltip>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent data-testid="logs-export-modal">
          <DialogTitle>Export logs</DialogTitle>
          <DialogDescription>Download matching log lines as a file.</DialogDescription>
          <div className="space-y-4">
            <fieldset className="space-y-2">
              <legend className="text-sm font-medium">Format</legend>
              <div className="flex items-center gap-2">
                <input
                  type="radio"
                  id="export-format-txt"
                  name="export-format"
                  data-testid="export-format-txt"
                  checked={format === 'txt'}
                  onChange={() => setFormat('txt')}
                />
                <Label htmlFor="export-format-txt">Text (.txt)</Label>
              </div>
              <div className="flex items-center gap-2">
                <input
                  type="radio"
                  id="export-format-json"
                  name="export-format"
                  data-testid="export-format-json"
                  checked={format === 'json'}
                  onChange={() => setFormat('json')}
                />
                <Label htmlFor="export-format-json">JSON (.json)</Label>
              </div>
            </fieldset>
            <div className="space-y-1">
              <Label htmlFor="export-max-lines">Max lines</Label>
              <Input
                id="export-max-lines"
                type="number"
                min={MIN_MAX}
                max={MAX_MAX}
                data-testid="export-max-lines"
                value={maxLines}
                onChange={(e) => {
                  const n = Number.parseInt(e.target.value, 10)
                  if (Number.isFinite(n)) {
                    setMaxLines(Math.min(MAX_MAX, Math.max(MIN_MAX, n)))
                  }
                }}
              />
            </div>
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button type="button" data-testid="export-download-button" onClick={handleDownload}>
              Download
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
