import type { ContainerRow } from './types'
import { ContainerGridRow } from './ContainerGridRow'

interface ContainerGridProps {
  containers: ContainerRow[]
}

export function ContainerGrid({ containers }: ContainerGridProps) {
  return (
    <div
      className="hidden overflow-x-auto rounded-md border border-border bg-card md:block"
      data-testid="containers-desktop"
    >
      <table className="min-w-full divide-y divide-border text-sm">
        <thead className="bg-muted/40 text-xs uppercase tracking-wide text-muted-foreground">
          <tr>
            <th scope="col" className="px-3 py-2 text-left">
              Compose
            </th>
            <th scope="col" className="px-3 py-2 text-left">
              Name
            </th>
            <th scope="col" className="px-3 py-2 text-left">
              Status
            </th>
            <th scope="col" className="px-3 py-2 text-right">
              Restarts (24h)
            </th>
            <th scope="col" className="px-3 py-2 text-left">
              Image
            </th>
            <th scope="col" className="px-3 py-2 text-left">
              CPU
            </th>
            <th scope="col" className="px-3 py-2 text-left">
              RAM
            </th>
            <th scope="col" className="px-3 py-2 text-left">
              Image Update
            </th>
            <th scope="col" className="px-3 py-2 text-left">
              Healthcheck
            </th>
            <th scope="col" className="px-3 py-2 text-left">
              Probes
            </th>
            <th scope="col" className="px-3 py-2 text-left">
              Logs
            </th>
            <th scope="col" className="px-3 py-2 text-left">
              Actions
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {containers.length === 0 ? (
            <tr>
              <td colSpan={12} className="px-3 py-6 text-center text-sm text-muted-foreground">
                No containers discovered yet.
              </td>
            </tr>
          ) : (
            containers.map((c) => <ContainerGridRow key={c.id} container={c} />)
          )}
        </tbody>
      </table>
    </div>
  )
}
