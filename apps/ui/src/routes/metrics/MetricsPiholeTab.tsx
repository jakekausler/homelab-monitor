import type { JSX } from 'react'

import { GrafanaDashboardEmbed } from './GrafanaDashboardEmbed'

export function MetricsPiholeTab(): JSX.Element {
  return (
    <GrafanaDashboardEmbed
      src="/api/grafana/d/pihole/pihole?kiosk"
      title="Pi-hole metrics (Grafana)"
    />
  )
}
