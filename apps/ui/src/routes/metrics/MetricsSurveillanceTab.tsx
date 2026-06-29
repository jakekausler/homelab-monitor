import type { JSX } from 'react'

import { GrafanaDashboardEmbed } from './GrafanaDashboardEmbed'

export function MetricsSurveillanceTab(): JSX.Element {
  return (
    <GrafanaDashboardEmbed
      src="/api/grafana/d/synology-surveillance/synology-surveillance?kiosk"
      title="Surveillance metrics (Grafana)"
    />
  )
}
