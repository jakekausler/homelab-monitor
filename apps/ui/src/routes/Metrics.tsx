/**
 * Metrics route — embeds Grafana at /api/grafana/ via a sandboxed iframe.
 *
 * Sandbox attribute: `allow-scripts allow-same-origin allow-forms`.
 * (Same caveat + same defenses as Alerts.tsx; see ErrorDisplay/CSP/proxy.)
 *
 * iframe src loads the host-overview dashboard in kiosk mode (no Grafana
 * navigation chrome). Operators needing the full Grafana UI (datasources,
 * plugins, alerts admin) can click the "Open in Grafana" link in the corner.
 *
 * Layout: full-bleed (negates AppShell's `p-6`), full-height of <main>.
 * Note: `h-[calc(100vh-3.5rem)]` assumes AppShell's TopBar is 3.5rem
 * (56px). If TopBar height changes, adjust this calc accordingly.
 * Note: identical h-calc coupling exists in apps/ui/src/routes/Alerts.tsx;
 * if AppShell's TopBar height changes, BOTH must be updated.
 */
export function MetricsPage() {
  return (
    <div className="-m-6 h-[calc(100vh-3.5rem)] relative">
      <iframe
        src="/api/grafana/d/host-overview/host-overview?kiosk"
        title="Metrics (Grafana)"
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
