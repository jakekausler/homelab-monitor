// Project test conventions:
// - Vitest (explicit imports), afterEach cleanup, render from @testing-library/react
// - Router wrapper: createRootRoute + createRoute + createRouter + RouterProvider
//   from @tanstack/react-router (mirrors CronDetailPage.test.tsx pattern)

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

import { SettingsLayout } from '@/routes/settings/SettingsLayout'

afterEach(() => {
  cleanup()
})

function renderSettingsLayout(initialPath = '/settings') {
  const rootRoute = createRootRoute({ component: () => <Outlet /> })
  const settingsRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: '/settings',
    component: SettingsLayout,
  })
  const logsRoute = createRoute({
    getParentRoute: () => settingsRoute,
    path: 'logs',
    component: () => <div>Logs page</div>,
  })
  const autofixRoute = createRoute({
    getParentRoute: () => settingsRoute,
    path: 'autofix',
    component: () => <div>Autofix page</div>,
  })
  const router = createRouter({
    routeTree: rootRoute.addChildren([settingsRoute.addChildren([logsRoute, autofixRoute])]),
    history: createMemoryHistory({ initialEntries: [initialPath] }),
  })
  render(<RouterProvider router={router} />)
}

describe('SettingsLayout', () => {
  it('renders the Settings heading', async () => {
    renderSettingsLayout()
    expect(await screen.findByText('Settings')).toBeInTheDocument()
  })

  it('renders the subtitle describing current capabilities', async () => {
    renderSettingsLayout()
    expect(await screen.findByText(/Logs retention and disk budget today/)).toBeInTheDocument()
  })

  it('renders both Logs and Auto-fix links', async () => {
    renderSettingsLayout()
    expect(await screen.findByTestId('settings-tab-logs')).toHaveTextContent('Logs')
    expect(screen.getByTestId('settings-tab-autofix')).toHaveTextContent('Auto-fix')
  })

  it('applies active styling to the Logs tab when on /settings/logs', async () => {
    renderSettingsLayout('/settings/logs')
    const logsTab = await screen.findByTestId('settings-tab-logs')
    const autofixTab = screen.getByTestId('settings-tab-autofix')

    expect(logsTab.className).toContain('bg-card')
    expect(logsTab.className).toContain('font-medium')
    expect(autofixTab.className).not.toContain('bg-card')
  })

  it('applies active styling to the Auto-fix tab when on /settings/autofix', async () => {
    renderSettingsLayout('/settings/autofix')
    const logsTab = await screen.findByTestId('settings-tab-logs')
    const autofixTab = screen.getByTestId('settings-tab-autofix')

    expect(autofixTab.className).toContain('bg-card')
    expect(autofixTab.className).toContain('font-medium')
    expect(logsTab.className).not.toContain('bg-card')
  })
})
