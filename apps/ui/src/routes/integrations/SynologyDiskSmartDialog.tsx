import type { JSX } from 'react'

import { useSynologyDiskSmart } from '@/api/synology'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { EmptyState } from '@/components/EmptyState'

import { QueryState } from './QueryState'
import type { SmartAttrRow } from './synologyFormat'

function formatCell(value: number | null): string {
  return value === null ? '—' : String(value)
}

function SmartAttrTable({ attrs }: { attrs: SmartAttrRow[] }): JSX.Element {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-border text-left text-xs text-muted-foreground">
            <th className="py-2 pr-3 font-medium">ID</th>
            <th className="py-2 pr-3 font-medium">Attribute</th>
            <th className="py-2 pr-3 font-medium">Raw</th>
            <th className="py-2 pr-3 font-medium">Worst</th>
            <th className="py-2 pr-3 font-medium">Threshold</th>
          </tr>
        </thead>
        <tbody>
          {attrs.map((row) => (
            <tr
              key={row.attr_id}
              className="border-b border-border/50"
              data-testid={`synology-smart-attr-${row.attr_id}`}
            >
              <td className="py-2 pr-3 text-muted-foreground">{row.attr_id}</td>
              <td className="py-2 pr-3">{row.attr_name}</td>
              <td className="py-2 pr-3 tabular-nums text-muted-foreground">
                {formatCell(row.raw)}
              </td>
              <td className="py-2 pr-3 tabular-nums text-muted-foreground">
                {formatCell(row.worst)}
              </td>
              <td className="py-2 pr-3 tabular-nums text-muted-foreground">
                {formatCell(row.threshold)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function SynologyDiskSmartDialog({
  disk,
  open,
  onOpenChange,
}: {
  disk: string
  open: boolean
  onOpenChange: (open: boolean) => void
}): JSX.Element {
  const result = useSynologyDiskSmart(disk)

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent data-testid="synology-smart-dialog">
        <DialogHeader>
          <DialogTitle>SMART attributes — {disk}</DialogTitle>
          <DialogDescription>Per-attribute SMART data from the SSH probe.</DialogDescription>
        </DialogHeader>
        <QueryState
          result={result}
          unavailableLabel="SMART attribute data temporarily unavailable"
          renderData={(data) => {
            if (data.attrs.length > 0) {
              return <SmartAttrTable attrs={data.attrs} />
            }
            if (data.data_available) {
              return (
                <EmptyState testId="synology-smart-empty-down">
                  SMART attributes unavailable for this disk — the SSH probe may be down, or no
                  attributes were collected.
                </EmptyState>
              )
            }
            return (
              <EmptyState testId="synology-smart-empty-nodata">
                No SMART attribute data collected for {disk}.
              </EmptyState>
            )
          }}
        />
      </DialogContent>
    </Dialog>
  )
}
