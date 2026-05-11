import { Link, useParams } from '@tanstack/react-router'
import { ArrowLeft } from 'lucide-react'

import { CronDetail } from '@/components/crons/CronDetail'

export function CronDetailPage() {
  const params = useParams({ strict: false })
  const cronId = params.cronId ?? ''

  return (
    <div className="space-y-4">
      <Link
        to="/inventory/crons"
        search={{
          page: 1,
          page_size: 100,
          host: undefined,
          integration_mode: undefined,
          enabled: undefined,
          state: undefined,
          q: undefined,
          include_archived: false,
        }}
        className="inline-flex items-center text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="mr-1 size-4" />
        Back to crons
      </Link>
      {cronId.length > 0 ? (
        <CronDetail cronId={cronId} />
      ) : (
        <p className="text-muted-foreground">Missing cron id.</p>
      )}
    </div>
  )
}
