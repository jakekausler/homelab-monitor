// STAGE-004-043 — read-only YAML preview. Wide viewports lazily load a CodeMirror
// YAML viewer; narrow viewports + the Suspense fallback render a plain read-only
// <textarea> (CodeMirror is non-functional in jsdom; tests mock useMediaQuery to
// force the textarea path, mirroring LogsQlEditor).

import { Suspense, lazy } from 'react'

import { useMediaQuery } from '@/lib/useMediaQuery'

export interface YamlPreviewProps {
  value: string
  ariaLabel?: string
  className?: string
}

const LazyYamlPreviewImpl = lazy(() => import('./YamlPreviewImpl'))

const TEXTAREA_CLASS =
  'w-full max-h-[60vh] resize-none overflow-auto rounded-md border border-input bg-muted px-3 py-2 font-mono text-[0.8125rem]'

function PlainYamlTextarea({ value, ariaLabel, className }: YamlPreviewProps) {
  return (
    <textarea
      data-testid="yaml-preview-textarea"
      aria-label={ariaLabel ?? 'Rule YAML preview'}
      readOnly
      rows={value.split('\n').length}
      className={className ?? TEXTAREA_CLASS}
      value={value}
    />
  )
}

export function YamlPreview(props: YamlPreviewProps) {
  const isWide = useMediaQuery('(min-width: 768px)')
  if (!isWide) {
    return <PlainYamlTextarea {...props} />
  }
  return (
    <Suspense fallback={<PlainYamlTextarea {...props} />}>
      <LazyYamlPreviewImpl
        value={props.value}
        ariaLabel={props.ariaLabel ?? 'Rule YAML preview'}
        {...(props.className !== undefined ? { className: props.className } : {})}
      />
    </Suspense>
  )
}
