import type { JSX } from 'react'

import { GrafanaDashboardEmbed } from './GrafanaDashboardEmbed'

export function MetricsHomeAssistantTab(): JSX.Element {
  return (
    <GrafanaDashboardEmbed
      src="/api/grafana/d/home-assistant/home-assistant?kiosk"
      title="Home Assistant metrics (Grafana)"
    />
  )
}
