import type { JSX } from 'react'

import { GrafanaDashboardEmbed } from './GrafanaDashboardEmbed'

export function MetricsHeartbeatsTab(): JSX.Element {
  return (
    <GrafanaDashboardEmbed
      src="/api/grafana/d/heartbeats/heartbeats?kiosk"
      title="Heartbeats metrics (Grafana)"
    />
  )
}
