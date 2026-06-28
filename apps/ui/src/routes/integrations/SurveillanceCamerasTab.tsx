import type { JSX } from 'react'

import { useSurveillanceCameras } from '@/api/surveillance'
import type { Schema } from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent } from '@/components/ui/card'
import { EmptyState } from '@/components/EmptyState'

import { QueryState } from './QueryState'
import { cameraConnectedBadge } from './surveillanceFormat'

type SurveillanceCameras = Schema<'SurveillanceCameras'>
type CameraRow = Schema<'CameraRow'>

/** Render a value or an em-dash for null/empty. */
function dash(value: string | number | null): string {
  if (value === null) return '—'
  if (typeof value === 'string' && value.length === 0) return '—'
  return String(value)
}

function CameraCard({ camera }: { camera: CameraRow }): JSX.Element {
  const conn = cameraConnectedBadge(camera.connected)
  return (
    <Card data-testid={`surveillance-camera-card-${camera.camera}`}>
      <CardContent className="space-y-2 p-4 text-sm">
        <div className="flex items-center justify-between gap-2">
          <h3 className="truncate font-medium">{camera.camera}</h3>
          <Badge variant={conn.variant}>{conn.label}</Badge>
        </div>
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-muted-foreground">
          <dt>Model</dt>
          <dd className="text-foreground">{dash(camera.model)}</dd>
          <dt>IP</dt>
          <dd className="text-foreground">{dash(camera.ip)}</dd>
          <dt>Vendor</dt>
          <dd className="text-foreground">{dash(camera.vendor)}</dd>
          <dt>Status</dt>
          <dd className="text-foreground">{dash(camera.status)}</dd>
          <dt>Recordings</dt>
          <dd className="text-foreground">{dash(camera.recordings_count)}</dd>
        </dl>
      </CardContent>
    </Card>
  )
}

export function SurveillanceCamerasTab(): JSX.Element {
  const cameras = useSurveillanceCameras()

  const renderCameras = (data: SurveillanceCameras): JSX.Element => {
    let body: JSX.Element
    if (!data.data_available) {
      body = (
        <EmptyState testId="surveillance-cameras-unavailable">
          No surveillance data yet — the collector has not run.
        </EmptyState>
      )
    } else if (data.cameras.length === 0) {
      body = <EmptyState testId="surveillance-cameras-empty">0 cameras</EmptyState>
    } else {
      body = (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {data.cameras.map((camera) => (
            <CameraCard key={camera.camera} camera={camera} />
          ))}
        </div>
      )
    }
    return body
  }

  return (
    <div className="h-full space-y-4 overflow-y-auto p-4">
      <QueryState
        result={cameras}
        unavailableLabel="Surveillance camera metrics temporarily unavailable"
        renderData={renderCameras}
      />
    </div>
  )
}
