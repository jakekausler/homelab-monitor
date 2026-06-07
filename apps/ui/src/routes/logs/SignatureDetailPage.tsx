import { useCallback, useState } from 'react'
import { Link, useNavigate, useParams } from '@tanstack/react-router'
import { ArrowLeft, Save } from 'lucide-react'
import type { JSX } from 'react'

import {
  useSignature,
  useSignatureSamples,
  useUpdateSignature,
  type SignaturePatchRequest,
} from '@/api/signatures'
import { Button } from '@/components/ui/button'
import { LogLineList } from '@/components/logs/LogLineList'
import { OpenInExplorerButton } from '@/components/logs/OpenInExplorerButton'
import { templateToLogsQl } from '@/lib/logsQlTranslate'
import { SignatureAnnotations } from './SignatureAnnotations'

export function SignatureDetailPage(): JSX.Element {
  const params = useParams({ strict: false })
  const templateHash = params.templateHash ?? ''
  const serviceKey = params.serviceKey ?? ''

  const navigate = useNavigate()

  const { data: sig } = useSignature(templateHash, serviceKey)
  const { data: samples, isLoading: samplesLoading } = useSignatureSamples(templateHash, serviceKey)
  const updateMut = useUpdateSignature()

  const [labelText, setLabelText] = useState(sig?.label ?? '')
  const [activeStatus, setActiveStatus] = useState<'active' | 'suppressed' | 'expected'>(
    sig?.status ?? 'active',
  )

  // Re-seed local edit state from the loaded signature when it first arrives and
  // whenever a different signature is selected. React-official "adjust state
  // during render" pattern, bounded by the seed-key compare (no loop, no
  // setState-in-effect — a build-failing react-hooks rule in this repo).
  const seedKey = sig ? `${templateHash} ${serviceKey}` : null
  const [prevSeedKey, setPrevSeedKey] = useState<string | null>(null)
  if (sig && seedKey !== prevSeedKey) {
    setPrevSeedKey(seedKey)
    setLabelText(sig.label ?? '')
    setActiveStatus(sig.status)
  }

  const handleSaveLabel = useCallback(() => {
    const body: SignaturePatchRequest = { label: labelText.length > 0 ? labelText : null }
    updateMut.mutate({ templateHash, serviceKey, body })
  }, [templateHash, serviceKey, labelText, updateMut])

  const handleStatusChange = useCallback(
    (status: 'active' | 'suppressed' | 'expected') => {
      setActiveStatus(status)
      const body: SignaturePatchRequest = { status }
      updateMut.mutate({ templateHash, serviceKey, body })
    },
    [templateHash, serviceKey, updateMut],
  )

  // Open in Explorer: anchor on the single LONGEST literal run of the template
  // (templateToLogsQl). AND-chaining every inter-wildcard segment over-constrains
  // the match and breaks when a trailing fragment carries non-printable bytes
  // (e.g. an ANSI reset), collapsing to zero results. The longest run is the most
  // distinctive identifier and reliably matches. (STAGE-004-031A Refinement.)
  const logsQl = sig !== undefined ? templateToLogsQl(sig.template_str) : ''

  // NOTE: do NOT scope the Explorer deep-link by service. The catalog
  // service_key is the VL `service` field, which spans multiple source_types
  // (docker, systemd, cron, ...). The Explorer's `services=` scope needs a
  // `<source_type>:<service>` identity, but the catalog row carries no
  // source_type, so the frontend cannot reconstruct a correct one — hardcoding
  // `docker:<key>` wrongly excluded every non-docker service (returned zero
  // logs). The `_msg` conjunction above is specific enough on its own. (The
  // backend samples endpoint DOES add a `service:"<key>"` filter — which works
  // there because it needs no source_type — but the Explorer deep-link is
  // intentionally broader and relies on the _msg match alone.)

  return (
    <div className="h-full min-h-0 space-y-4 overflow-auto p-4" data-testid="signature-detail-page">
      <Link
        to="/logs/signatures"
        search={{ service: undefined, status: undefined, label_q: undefined }}
        className="inline-flex items-center text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="mr-1 size-4" />
        Back to signatures
      </Link>

      {/* Full template */}
      {sig && (
        <div>
          <h3 className="mb-2 text-sm font-semibold">Template</h3>
          <pre className="whitespace-pre-wrap break-all rounded-md border border-border bg-muted/30 p-3 text-xs font-mono">
            {sig.template_str}
          </pre>
        </div>
      )}

      {/* Sample lines */}
      <div>
        <h3 className="mb-2 text-sm font-semibold">Recent Samples</h3>
        {samplesLoading ? (
          <div className="text-xs text-muted-foreground">Loading samples...</div>
        ) : samples?.reason === 'template_too_generic' ? (
          <div className="text-xs text-muted-foreground">
            Template too generic for live samples.
          </div>
        ) : samples?.reason === 'vl_unavailable' ? (
          <div className="text-xs text-muted-foreground">Sample logs temporarily unavailable.</div>
        ) : samples?.lines && samples.lines.length > 0 ? (
          <LogLineList lines={samples.lines} wrap testId="signature-samples" />
        ) : (
          <div className="text-xs text-muted-foreground">No recent matches.</div>
        )}
      </div>

      {/* Annotations */}
      {sig && <SignatureAnnotations templateHash={templateHash} serviceKey={serviceKey} />}

      {/* Label edit */}
      {sig && (
        <div>
          <h3 className="mb-2 text-sm font-semibold">Label</h3>
          <div className="flex gap-2">
            <input
              type="text"
              value={labelText}
              onChange={(e) => setLabelText(e.currentTarget.value)}
              placeholder="Add a label..."
              className="flex-1 rounded-md border border-border bg-background px-2 py-1 text-sm"
              disabled={updateMut.isPending}
            />
            <Button
              size="sm"
              onClick={handleSaveLabel}
              disabled={updateMut.isPending || labelText === (sig.label ?? '')}
            >
              <Save className="mr-1 size-4" />
              Save
            </Button>
          </div>
        </div>
      )}

      {/* Status toggle */}
      {sig && (
        <div>
          <h3 className="mb-2 text-sm font-semibold">Status</h3>
          <div className="flex gap-2">
            {(['active', 'suppressed', 'expected'] as const).map((status) => (
              <Button
                key={status}
                size="sm"
                variant={activeStatus === status ? 'default' : 'outline'}
                onClick={() => handleStatusChange(status)}
                disabled={updateMut.isPending}
              >
                {status}
              </Button>
            ))}
          </div>
        </div>
      )}

      {/* Open in Explorer button */}
      {sig && logsQl.length > 0 && (
        <div>
          <OpenInExplorerButton logsQl={logsQl} />
        </div>
      )}

      {/* View model button */}
      {sig && (
        <div>
          <button
            type="button"
            onClick={() => {
              void navigate({
                to: '/logs/models-debug',
                search: { model: sig.service_key },
              })
            }}
            className="inline-flex items-center rounded-md border border-border px-3 py-1.5 text-sm hover:bg-accent"
            data-testid="view-model-link"
          >
            Open in Models
          </button>
        </div>
      )}
    </div>
  )
}
