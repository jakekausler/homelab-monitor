import { Link, useParams } from '@tanstack/react-router'
import { ArrowLeft } from 'lucide-react'

import { CronDetail } from '@/components/crons/CronDetail'

export function CronDetailPage() {
  const params = useParams({ strict: false })
  const fingerprint = params.fingerprint ?? ''

  return (
    <div className="space-y-4">
      <Link
        to="/inventory/crons"
        search={{
          page: 1,
          page_size: 100,
          host: undefined,
          enabled: undefined,
          state: undefined,
          q: undefined,
          include_hidden: false,
        }}
        className="inline-flex items-center text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="mr-1 size-4" />
        Back to crons
      </Link>
      {fingerprint.length > 0 ? (
        <CronDetail fingerprint={fingerprint} />
      ) : (
        <p className="text-muted-foreground">Missing cron fingerprint.</p>
      )}
    </div>
  )
}
