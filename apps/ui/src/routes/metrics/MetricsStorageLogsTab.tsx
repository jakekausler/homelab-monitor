import type { JSX } from 'react'

import { GrafanaDashboardEmbed } from './GrafanaDashboardEmbed'

export function MetricsStorageLogsTab(): JSX.Element {
  return (
    <GrafanaDashboardEmbed
      src="/api/grafana/d/storage-logs/storage-logs?kiosk"
      title="Storage & Logs metrics (Grafana)"
    />
  )
}
