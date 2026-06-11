/**
 * Active Alerts tab — embeds Karma at /api/karma/ via a sandboxed iframe.
 *
 * Sandbox attribute: `allow-scripts allow-same-origin allow-forms`.
 * Caveat: `allow-same-origin` largely cancels other restrictions because
 * the iframe is loaded from our own origin (Karma's SPA fetches from
 * `/api/karma/api/v2/...` with our cookies). The sandbox is mostly
 * cosmetic in this configuration. The REAL defenses are:
 *   1. CSP `frame-ancestors 'self'` blocks third-party embeds.
 *   2. The monitor's reverse-proxy auth gate (require_session_no_csrf).
 *   3. Origin/Referer same-origin enforcement on state-changing methods.
 *
 * Layout: fills the AlertsLayout <Outlet> region (h-full of the flex child).
 */
export function ActiveAlertsTab() {
  return (
    <div className="h-full w-full">
      <iframe
        src="/api/karma/"
        title="Alerts (Karma)"
        // STAGE-001-019: allow-scripts + allow-same-origin is the locked package
        // from Design — equivalent to no sandbox for same-origin content but
        // still blocks popups/top-nav/downloads. CSP frame-ancestors 'self' and
        // server-side Origin/Referer enforcement are the real defense layers.
        // eslint-disable-next-line @eslint-react/dom-no-unsafe-iframe-sandbox
        sandbox="allow-scripts allow-same-origin allow-forms"
        className="block h-full w-full border-0"
      />
    </div>
  )
}
