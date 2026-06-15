import type { JSX } from 'react'

import { GrafanaDashboardEmbed } from './GrafanaDashboardEmbed'

export function MetricsCollectorsTab(): JSX.Element {
  return (
    <GrafanaDashboardEmbed
      src="/api/grafana/d/collectors/collectors?kiosk"
      title="Collectors metrics (Grafana)"
    />
  )
}
