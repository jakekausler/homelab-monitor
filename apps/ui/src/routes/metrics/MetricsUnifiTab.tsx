import type { JSX } from 'react'

import { GrafanaDashboardEmbed } from './GrafanaDashboardEmbed'

export function MetricsUnifiTab(): JSX.Element {
  return (
    <GrafanaDashboardEmbed
      src="/api/grafana/d/homelab-unifi/homelab-unifi?kiosk"
      title="Unifi metrics (Grafana)"
    />
  )
}
