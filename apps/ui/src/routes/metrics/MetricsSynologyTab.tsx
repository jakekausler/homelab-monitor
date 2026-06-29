import type { JSX } from 'react'

import { GrafanaDashboardEmbed } from './GrafanaDashboardEmbed'

export function MetricsSynologyTab(): JSX.Element {
  return (
    <GrafanaDashboardEmbed
      src="/api/grafana/d/synology/synology?kiosk"
      title="Synology metrics (Grafana)"
    />
  )
}
