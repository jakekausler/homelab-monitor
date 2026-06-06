import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  Outlet,
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from '@tanstack/react-router'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/signatures', () => ({
  useSignature: vi.fn(),
  useSignatureSamples: vi.fn(),
  useUpdateSignature: vi.fn(),
}))

vi.mock('@/api/annotations', () => ({
  useSignatureAnnotations: vi.fn(() => ({ data: { annotations: [] } })),
  useAddAnnotation: vi.fn(() => ({ isPending: false, mutate: vi.fn() })),
  useDeleteAnnotation: vi.fn(() => ({ isPending: false, mutate: vi.fn() })),
}))

// OpenInExplorerButton uses a TanStack <Link>; mock it — collaborator, not the
// unit under test here.
vi.mock('@/components/logs/OpenInExplorerButton', () => ({
  OpenInExplorerButton: () => <button type="button">Open in Explorer</button>,
}))

import { useSignature, useSignatureSamples, useUpdateSignature } from '@/api/signatures'
import { SignatureDetailPage } from '../SignatureDetailPage'

const mockUseSignature = vi.mocked(useSignature)
const mockUseSignatureSamples = vi.mocked(useSignatureSamples)
const mockUseUpdateSignature = vi.mocked(useUpdateSignature)

afterEach(cleanup)

function renderPage(templateHash = 'hash1', serviceKey = 'docker:nginx') {
  const rootRoute = createRootRoute({ component: () => <Outlet /> })
  const logsRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: '/logs',
    component: () => <Outlet />,
  })
  const listRoute = createRoute({
    getParentRoute: () => logsRoute,
    path: 'signatures',
    component: () => <div>Signatures list</div>,
    validateSearch: (): { service?: string; status?: string; label_q?: string } => ({}),
  })
  const detailRoute = createRoute({
    getParentRoute: () => logsRoute,
    path: 'signatures/$templateHash/$serviceKey',
    component: SignatureDetailPage,
  })
  const modelsDebugRoute = createRoute({
    getParentRoute: () => logsRoute,
    path: 'models-debug',
    component: () => <div data-testid="models-debug-page">Models</div>,
    validateSearch: (search: Record<string, unknown>): { model?: string | undefined } => ({
      ...(typeof search.model === 'string' ? { model: search.model } : {}),
    }),
  })
  const router = createRouter({
    routeTree: rootRoute.addChildren([
      logsRoute.addChildren([listRoute, detailRoute, modelsDebugRoute]),
    ]),
    history: createMemoryHistory({
      initialEntries: [`/logs/signatures/${templateHash}/${serviceKey}`],
    }),
  })
  const qc = new QueryClient()
  return render(
    <QueryClientProvider client={qc}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

describe('SignatureDetailPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  const baseSig = {
    template_hash: 'hash1',
    service_key: 'docker:nginx',
    template_str: 'error <*> occurred in service',
    label: 'test',
    status: 'active' as const,
    first_seen_at: 1000,
    last_seen_at: 2000,
    total_count: 42,
  }

  function setMocks(sig: typeof baseSig | undefined, samples: unknown, pending = false) {
    mockUseSignature.mockReturnValue({ data: sig } as unknown as ReturnType<typeof useSignature>)
    mockUseSignatureSamples.mockReturnValue(samples as ReturnType<typeof useSignatureSamples>)
    mockUseUpdateSignature.mockReturnValue({
      isPending: pending,
      mutate: vi.fn(),
    } as unknown as ReturnType<typeof useUpdateSignature>)
  }

  it('renders the full template in a pre element', async () => {
    setMocks(baseSig, { data: { lines: [], reason: null }, isLoading: false })
    renderPage()
    const matches = await screen.findAllByText(/error.*occurred in service/)
    expect(matches.length).toBeGreaterThan(0)
  })

  it('renders a Back link to the signatures list', async () => {
    setMocks(baseSig, { data: { lines: [], reason: null }, isLoading: false })
    renderPage()
    expect(await screen.findByRole('link', { name: /Back to signatures/i })).toBeInTheDocument()
  })

  it('renders the full-page container (no aside)', async () => {
    setMocks(baseSig, { data: { lines: [], reason: null }, isLoading: false })
    renderPage()
    expect(await screen.findByTestId('signature-detail-page')).toBeInTheDocument()
    expect(screen.queryByTestId('signature-detail-aside')).not.toBeInTheDocument()
  })

  it('renders sample lines via LogLineList', async () => {
    setMocks(baseSig, {
      data: {
        lines: [{ timestamp: '2024-01-01T00:00:00Z', level: 'info', message: 'm', fields: {} }],
        reason: null,
      },
      isLoading: false,
    })
    renderPage()
    expect(await screen.findByTestId('signature-samples')).toBeInTheDocument()
  })

  it('renders template_too_generic reason', async () => {
    setMocks(
      { ...baseSig, template_str: '<*>' },
      {
        data: { lines: [], reason: 'template_too_generic' },
        isLoading: false,
      },
    )
    renderPage()
    expect(await screen.findByText('Template too generic for live samples.')).toBeInTheDocument()
  })

  it('renders vl_unavailable reason', async () => {
    setMocks(baseSig, { data: { lines: [], reason: 'vl_unavailable' }, isLoading: false })
    renderPage()
    expect(await screen.findByText('Sample logs temporarily unavailable.')).toBeInTheDocument()
  })

  it('renders label edit input and Save button', async () => {
    setMocks(
      { ...baseSig, label: 'existing-label' },
      {
        data: { lines: [], reason: null },
        isLoading: false,
      },
    )
    renderPage()
    expect(await screen.findByDisplayValue('existing-label')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Save/ })).toBeInTheDocument()
  })

  it('fires mutate when saving a label', async () => {
    const mutate = vi.fn()
    mockUseSignature.mockReturnValue({
      data: { ...baseSig, label: 'old' },
    } as unknown as ReturnType<typeof useSignature>)
    mockUseSignatureSamples.mockReturnValue({
      data: { lines: [], reason: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useSignatureSamples>)
    mockUseUpdateSignature.mockReturnValue({
      isPending: false,
      mutate,
    } as unknown as ReturnType<typeof useUpdateSignature>)
    renderPage()
    const input = await screen.findByDisplayValue('old')
    fireEvent.change(input, { target: { value: 'new-label' } })
    fireEvent.click(screen.getByRole('button', { name: /Save/ }))
    expect(mutate).toHaveBeenCalledWith(expect.objectContaining({ body: { label: 'new-label' } }))
  })

  it('renders status toggle buttons and fires mutate on click', async () => {
    const mutate = vi.fn()
    mockUseSignature.mockReturnValue({ data: baseSig } as unknown as ReturnType<
      typeof useSignature
    >)
    mockUseSignatureSamples.mockReturnValue({
      data: { lines: [], reason: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useSignatureSamples>)
    mockUseUpdateSignature.mockReturnValue({
      isPending: false,
      mutate,
    } as unknown as ReturnType<typeof useUpdateSignature>)
    renderPage()
    expect(await screen.findByRole('button', { name: 'suppressed' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'suppressed' }))
    expect(mutate).toHaveBeenCalledWith(expect.objectContaining({ body: { status: 'suppressed' } }))
  })

  it('renders loading state for samples', async () => {
    setMocks(baseSig, { data: undefined, isLoading: true })
    renderPage()
    expect(await screen.findByText('Loading samples...')).toBeInTheDocument()
  })

  it('renders "No recent matches" when lines empty and reason null', async () => {
    setMocks(baseSig, { data: { lines: [], reason: null }, isLoading: false })
    renderPage()
    expect(await screen.findByText('No recent matches.')).toBeInTheDocument()
  })

  it('renders "Open in Models" button that navigates to models-debug with model=service_key', async () => {
    setMocks(baseSig, { data: { lines: [], reason: null }, isLoading: false })
    renderPage()
    const btn = await screen.findByTestId('view-model-link')
    expect(btn).toBeInTheDocument()
    // Clicking navigates — verify the button is clickable without error
    fireEvent.click(btn)
    // After navigation, models-debug page renders (router navigated)
    expect(await screen.findByTestId('models-debug-page')).toBeInTheDocument()
  })
})
