import type { JSX } from 'react'

import { useUnifiDhcp, useUnifiDnsPosture, useUnifiWan, useUnifiWifi } from '@/api/unifi'
import { UnifiRangeChart } from '@/components/charts/UnifiRangeChart'

import { NetworkDhcpWidget } from './NetworkDhcpWidget'
import { NetworkDnsPostureWidget } from './NetworkDnsPostureWidget'
import { NetworkSsidWidget } from './NetworkSsidWidget'
import { NetworkWanWidget } from './NetworkWanWidget'
import { NetworkWifiWidget } from './NetworkWifiWidget'
import { PanelSection } from './PanelSection'
import { QueryState } from './QueryState'

function formatMbpsAxis(value: number): string {
  return `${value.toFixed(0)}`
}

function formatLatencyAxis(value: number): string {
  // Latency metric is seconds; show ms.
  return `${(value * 1000).toFixed(0)} ms`
}

export function NetworkOverviewTab(): JSX.Element {
  const wan = useUnifiWan()
  const dhcp = useUnifiDhcp()
  const wifi = useUnifiWifi()
  const dnsPosture = useUnifiDnsPosture()

  return (
    <div className="h-full space-y-4 overflow-y-auto p-4">
      <PanelSection title="WAN / ISP">
        <QueryState
          result={wan}
          unavailableLabel="WAN data temporarily unavailable"
          renderData={(data) => <NetworkWanWidget wan={data} />}
        />
        <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-2">
          <UnifiRangeChart
            title="Speedtest (Mbps)"
            valueFormatter={formatMbpsAxis}
            series={[
              {
                expr: 'homelab_unifi_speedtest_download_mbps',
                label: 'Download',
                color: '#2563eb',
              },
              { expr: 'homelab_unifi_speedtest_upload_mbps', label: 'Upload', color: '#16a34a' },
            ]}
          />
          <UnifiRangeChart
            title="WAN latency"
            valueFormatter={formatLatencyAxis}
            series={[
              { expr: 'homelab_unifi_wan_latency_seconds', label: 'Latency', color: '#d97706' },
            ]}
          />
        </div>
      </PanelSection>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <PanelSection title="DHCP">
          <QueryState
            result={dhcp}
            unavailableLabel="DHCP data temporarily unavailable"
            renderData={(data) => <NetworkDhcpWidget data={data} />}
          />
        </PanelSection>

        <PanelSection title="WiFi experience">
          <QueryState
            result={wifi}
            unavailableLabel="WiFi data temporarily unavailable"
            renderData={(data) => <NetworkWifiWidget data={data} />}
          />
        </PanelSection>

        <PanelSection title="SSID distribution">
          <QueryState
            result={wifi}
            unavailableLabel="WiFi data temporarily unavailable"
            renderData={(data) => <NetworkSsidWidget ssids={data.ssids} />}
          />
        </PanelSection>

        <PanelSection title="DNS posture">
          <QueryState
            result={dnsPosture}
            unavailableLabel="DNS posture temporarily unavailable"
            renderData={(data) => <NetworkDnsPostureWidget data={data} />}
          />
        </PanelSection>
      </div>
    </div>
  )
}
