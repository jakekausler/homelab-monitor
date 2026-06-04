import {
  Outlet,
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from '@tanstack/react-router'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { OpenInExplorerButton } from '@/components/logs/OpenInExplorerButton'
import type { OpenInExplorerButtonProps } from '@/components/logs/OpenInExplorerButton'

afterEach(cleanup)

// Minimal router: a root that renders the button (via a wrapper component) plus a
// catch-all /logs route so the generated link resolves to a real route. We render
// the button on the index ('/') route and assert the anchor's href; we do NOT
// need to navigate.
function renderButton(props: OpenInExplorerButtonProps) {
  const rootRoute = createRootRoute({ component: () => <Outlet /> })
  const indexRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: '/',
    component: () => <OpenInExplorerButton {...props} />,
  })
  const logsRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: '/logs',
    component: () => <div>logs</div>,
    validateSearch: (
      search: Record<string, unknown>,
    ): {
      q?: string | undefined
      logsql?: string | undefined
      since?: string | undefined
      start?: string | undefined
      end?: string | undefined
      services?: string | undefined
    } => ({
      q: typeof search.q === 'string' ? search.q : undefined,
      logsql: typeof search.logsql === 'string' ? search.logsql : undefined,
      since: typeof search.since === 'string' ? search.since : undefined,
      start: typeof search.start === 'string' ? search.start : undefined,
      end: typeof search.end === 'string' ? search.end : undefined,
      services: typeof search.services === 'string' ? search.services : undefined,
    }),
  })
  const router = createRouter({
    routeTree: rootRoute.addChildren([indexRoute, logsRoute]),
    history: createMemoryHistory({ initialEntries: ['/'] }),
  })
  return render(<RouterProvider router={router} />)
}

function anchorHref(): string {
  const el = screen.getByTestId('open-in-explorer')
  const anchor = el.closest('a')
  expect(anchor).not.toBeNull()
  return anchor!.getAttribute('href') ?? ''
}

describe('OpenInExplorerButton', () => {
  it('renders an anchor (SPA Link) with the default label', async () => {
    renderButton({ sincePreset: '1h' })
    const el = await screen.findByTestId('open-in-explorer')
    expect(el.closest('a')).not.toBeNull()
    expect(el).toHaveTextContent('Open in Explorer')
  })

  it('respects a custom label', async () => {
    renderButton({ sincePreset: '1h', label: 'View all logs' })
    expect(await screen.findByTestId('open-in-explorer')).toHaveTextContent('View all logs')
  })

  it('builds a docker-shape href (service logsQl + preset)', async () => {
    renderButton({ logsQl: 'service:"nginx"', sincePreset: '15m' })
    await screen.findByTestId('open-in-explorer')
    const href = anchorHref()
    const params = new URLSearchParams(href.split('?')[1])
    expect(params.get('logsql')).toBe('service:"nginx"')
    expect(params.get('since')).toBe('15m')
    expect(href.startsWith('/logs?')).toBe(true)
  })

  it('builds a cron-shape href (fingerprint+run_id logsQl + custom range)', async () => {
    const start = new Date('2026-05-01T12:00:00.000Z')
    const end = new Date('2026-05-01T12:00:30.000Z')
    renderButton({
      logsQl: 'cron_fingerprint:"fp123" AND run_id:"run-abc"',
      rangeStart: start,
      rangeEnd: end,
    })
    await screen.findByTestId('open-in-explorer')
    const href = anchorHref()
    const params = new URLSearchParams(href.split('?')[1])
    expect(params.get('logsql')).toBe('cron_fingerprint:"fp123" AND run_id:"run-abc"')
    expect(params.get('start')).toBe('2026-05-01T12:00:00.000Z')
    expect(params.get('end')).toBe('2026-05-01T12:00:30.000Z')
  })
})
