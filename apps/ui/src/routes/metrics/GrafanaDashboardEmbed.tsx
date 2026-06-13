/**
 * Shared Grafana dashboard embed — a sandboxed iframe in a full-bleed wrapper,
 * with an "Open in Grafana" link in the corner.
 *
 * Extracted from the original MetricsPage (STAGE-001-020). Both Metrics tabs
 * (System, Home Assistant) render this with their own dashboard `src`.
 *
 * Sandbox attribute: `allow-scripts allow-same-origin allow-forms`.
 * (Same caveat + same defenses as ActiveAlertsTab's Karma iframe; see CSP/proxy.)
 *
 * iframe src loads a dashboard in kiosk mode (no Grafana navigation chrome).
 * Operators needing the full Grafana UI (datasources, plugins, alerts admin)
 * can click the "Open in Grafana" link in the corner.
 *
 * Layout: fills the MetricsLayout Outlet region via `h-full` (MetricsLayout
 * provides a bounded `min-h-0 flex-1 overflow-hidden` flex child as the outlet
 * wrapper, so `h-full` resolves without a viewport calc).
 */
import type { JSX } from 'react'

export interface GrafanaDashboardEmbedProps {
  /** The proxied Grafana dashboard URL, e.g. `/api/grafana/d/host-overview/host-overview?kiosk` */
  src: string
  /** Accessible iframe title, e.g. `System metrics (Grafana)` */
  title: string
}

export function GrafanaDashboardEmbed({ src, title }: GrafanaDashboardEmbedProps): JSX.Element {
  return (
    <div className="relative h-full w-full">
      <iframe
        src={src}
        title={title}
        // STAGE-001-020 design decision (mirrors STAGE-019 Alerts iframe):
        //  allow-scripts + allow-same-origin is the locked package.
        //  CSP frame-ancestors 'self' and server-side Origin/Referer are
        //  the real defense layers.
        // eslint-disable-next-line @eslint-react/dom-no-unsafe-iframe-sandbox
        sandbox="allow-scripts allow-same-origin allow-forms"
        className="block h-full w-full border-0"
      />
      <a
        href="/api/grafana/"
        target="_blank"
        rel="noopener noreferrer"
        className="absolute bottom-2 right-2 text-xs text-muted-foreground hover:text-foreground bg-background/80 px-2 py-1 rounded"
      >
        Open in Grafana ↗
      </a>
    </div>
  )
}
