import type { JSX } from 'react'

import { GrafanaDashboardEmbed } from './GrafanaDashboardEmbed'

export function MetricsNetworkTab(): JSX.Element {
  return (
    <GrafanaDashboardEmbed
      src="/api/grafana/d/network/network?kiosk"
      title="Network metrics (Grafana)"
    />
  )
}
