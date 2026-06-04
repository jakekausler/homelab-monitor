// Project test conventions:
// - Framework: Vitest (no globals — explicit imports)
// - Component render: @testing-library/react
// - afterEach(cleanup) per convention

import React from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ExportButton, buildExportUrl } from '@/routes/logs/ExportButton'
import { TooltipProvider } from '@/components/ui/tooltip'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

const PROPS = {
  expr: '_msg:"error"',
  startIso: '2026-05-07T00:00:00Z',
  endIso: '2026-05-07T01:00:00Z',
  servicesCsv: '',
}

describe('buildExportUrl', () => {
  it('builds a same-origin /api/logs/export URL with encoded params', () => {
    const url = buildExportUrl({
      expr: '_msg:"error"',
      startIso: '2026-05-07T00:00:00Z',
      endIso: '2026-05-07T01:00:00Z',
      format: 'txt',
      max: 500,
      servicesCsv: '',
    })
    expect(url.startsWith('/api/logs/export?')).toBe(true)
    const params = new URLSearchParams(url.split('?')[1])
    expect(params.get('expr')).toBe('_msg:"error"')
    expect(params.get('start')).toBe('2026-05-07T00:00:00Z')
    expect(params.get('end')).toBe('2026-05-07T01:00:00Z')
    expect(params.get('format')).toBe('txt')
    expect(params.get('max')).toBe('500')
    expect(params.has('services')).toBe(false) // omitted when empty
  })

  it('includes services when non-empty', () => {
    const url = buildExportUrl({
      expr: '*',
      startIso: 'a',
      endIso: 'b',
      format: 'json',
      max: 100,
      servicesCsv: 'docker:nginx',
    })
    const params = new URLSearchParams(url.split('?')[1])
    expect(params.get('services')).toBe('docker:nginx')
    expect(params.get('format')).toBe('json')
  })

  it('clamps max into [1, 100000]', () => {
    const lo = new URLSearchParams(
      buildExportUrl({
        expr: '*',
        startIso: 'a',
        endIso: 'b',
        format: 'txt',
        max: 0,
        servicesCsv: '',
      }).split('?')[1],
    )
    expect(lo.get('max')).toBe('1')
    const hi = new URLSearchParams(
      buildExportUrl({
        expr: '*',
        startIso: 'a',
        endIso: 'b',
        format: 'txt',
        max: 999999,
        servicesCsv: '',
      }).split('?')[1],
    )
    expect(hi.get('max')).toBe('100000')
  })
})

const renderWithProvider = (ui: React.ReactElement) =>
  render(<TooltipProvider>{ui}</TooltipProvider>)

describe('ExportButton', () => {
  it('renders the export button', () => {
    renderWithProvider(<ExportButton {...PROPS} />)
    expect(screen.getByTestId('logs-export-button')).toBeInTheDocument()
  })

  it('opens the modal on click with format radios and max input', () => {
    renderWithProvider(<ExportButton {...PROPS} />)
    fireEvent.click(screen.getByTestId('logs-export-button'))
    expect(screen.getByTestId('logs-export-modal')).toBeInTheDocument()
    const txt = screen.getByTestId<HTMLInputElement>('export-format-txt')
    const json = screen.getByTestId<HTMLInputElement>('export-format-json')
    expect(txt.checked).toBe(true) // default txt
    expect(json.checked).toBe(false)
    const max = screen.getByTestId<HTMLInputElement>('export-max-lines')
    expect(max.value).toBe('10000') // default
  })

  it('clicking Download builds the correct URL and triggers an anchor download', () => {
    // Capture the href the component assigns to the hidden anchor.
    const realCreate = document.createElement.bind(document)
    let capturedHref = ''
    const clickSpy = vi.fn()
    vi.spyOn(document, 'createElement').mockImplementation((tag: string) => {
      const el = realCreate(tag)
      if (tag === 'a') {
        // Intercept click + capture href at click time.
        Object.defineProperty(el, 'click', { value: clickSpy })
        const anchor = el as HTMLAnchorElement
        const origSetAttr = anchor.setAttribute.bind(anchor)
        anchor.setAttribute = (name: string, value: string) => origSetAttr(name, value)
        // href is set via assignment in the component; read it on click.
        clickSpy.mockImplementation(() => {
          capturedHref = anchor.getAttribute('href') ?? ''
        })
      }
      return el
    })

    renderWithProvider(<ExportButton {...PROPS} />)
    fireEvent.click(screen.getByTestId('logs-export-button'))
    fireEvent.click(screen.getByTestId('export-download-button'))

    expect(clickSpy).toHaveBeenCalledTimes(1)
    expect(capturedHref.startsWith('/api/logs/export?')).toBe(true)
    const params = new URLSearchParams(capturedHref.split('?')[1])
    expect(params.get('expr')).toBe(PROPS.expr)
    expect(params.get('format')).toBe('txt')
    expect(params.has('services')).toBe(false)
  })

  it('Cancel closes the modal', () => {
    renderWithProvider(<ExportButton {...PROPS} />)
    fireEvent.click(screen.getByTestId('logs-export-button'))
    expect(screen.getByTestId('logs-export-modal')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    // Radix unmounts content on close; assert it's gone.
    expect(screen.queryByTestId('logs-export-modal')).not.toBeInTheDocument()
  })
})
