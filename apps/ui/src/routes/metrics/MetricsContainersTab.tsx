import type { JSX } from 'react'

import { GrafanaDashboardEmbed } from './GrafanaDashboardEmbed'

export function MetricsContainersTab(): JSX.Element {
  return (
    <GrafanaDashboardEmbed
      src="/api/grafana/d/containers/containers?kiosk"
      title="Containers metrics (Grafana)"
    />
  )
}
