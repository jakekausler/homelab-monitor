import type { JSX } from 'react'

import { GrafanaDashboardEmbed } from './GrafanaDashboardEmbed'

export function MetricsSystemTab(): JSX.Element {
  return (
    <GrafanaDashboardEmbed
      src="/api/grafana/d/host-overview/host-overview?kiosk"
      title="System metrics (Grafana)"
    />
  )
}
