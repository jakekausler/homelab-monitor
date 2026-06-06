import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/models', () => ({
  useModelsList: vi.fn(),
  useModelDetail: vi.fn(),
  useLastCycle: vi.fn(),
  useTriggerRefresh: vi.fn(),
}))

const mockNavigate = vi.fn()
vi.mock('@tanstack/react-router', async () => {
  const actual = await vi.importActual('@tanstack/react-router')
  return {
    ...actual,
    useNavigate: () => mockNavigate,
    useSearch: () => ({}),
  }
})

vi.mock('@/components/logs/OpenInExplorerButton', () => ({
  OpenInExplorerButton: () => (
    <button type="button" data-testid="open-in-explorer">
      Open in Explorer
    </button>
  ),
}))

vi.mock('@/lib/logsQlTranslate', () => ({
  msgFilterClause: (seg: string) => (seg.trim().length > 0 ? `_msg:"${seg}"` : null),
}))

import { useModelDetail, useModelsList } from '@/api/models'
import { ModelsDebugPage } from '../ModelsDebugPage'

const mockUseModelsList = vi.mocked(useModelsList)
const mockUseModelDetail = vi.mocked(useModelDetail)

afterEach(() => {
  cleanup()
})

const emptyDetailReturn = {
  data: undefined,
  isLoading: false,
  isSuccess: false,
  isError: false,
  error: null,
  fetchStatus: 'idle' as const,
} as unknown as ReturnType<typeof useModelDetail>

describe('ModelsDebugPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockNavigate.mockClear()
    // Default detail to idle/no data
    mockUseModelDetail.mockReturnValue(emptyDetailReturn)
  })

  it('renders the model keys from useModelsList in the desktop sidebar', () => {
    mockUseModelsList.mockReturnValue({
      data: {
        models: [
          {
            model_key: 'docker:nginx',
            template_count: 3,
            line_count: 100,
            last_processed_ts: 1000,
            updated_at: 2000,
          },
          {
            model_key: 'cron:backup',
            template_count: 1,
            line_count: 10,
            last_processed_ts: null,
            updated_at: 3000,
          },
        ],
      },
      isLoading: false,
      isSuccess: true,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useModelsList>)

    render(<ModelsDebugPage />)

    const items = screen.getAllByTestId('model-list-item')
    expect(items.length).toBe(2)
    expect(items[0]).toHaveAttribute('data-model-key', 'docker:nginx')
    expect(items[1]).toHaveAttribute('data-model-key', 'cron:backup')
  })

  it('renders template_count badges for each model key', () => {
    mockUseModelsList.mockReturnValue({
      data: {
        models: [
          {
            model_key: 'docker:nginx',
            template_count: 7,
            line_count: 50,
            last_processed_ts: 1000,
            updated_at: 2000,
          },
        ],
      },
      isLoading: false,
      isSuccess: true,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useModelsList>)

    render(<ModelsDebugPage />)

    expect(screen.getAllByText('7').length).toBeGreaterThan(0)
  })

  it('clicking a model-list-item renders the template table for that model', () => {
    mockUseModelsList.mockReturnValue({
      data: {
        models: [
          {
            model_key: 'docker:nginx',
            template_count: 1,
            line_count: 10,
            last_processed_ts: 1000,
            updated_at: 2000,
          },
        ],
      },
      isLoading: false,
      isSuccess: true,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useModelsList>)

    mockUseModelDetail.mockReturnValue({
      data: {
        model_key: 'docker:nginx',
        summary: {
          model_key: 'docker:nginx',
          template_count: 1,
          line_count: 10,
          last_processed_ts: 1000,
          updated_at: 2000,
        },
        templates: [
          {
            template_id: 42,
            template_hash: 'abc12345',
            template_str: 'error <*> occurred',
            size: 3,
            first_seen_ts: 1_700_000_000_000,
            last_seen_ts: 1_700_001_000_000,
          },
        ],
      },
      isLoading: false,
      isSuccess: true,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useModelDetail>)

    render(<ModelsDebugPage />)

    fireEvent.click(screen.getByTestId('model-list-item'))

    const table = screen.getByTestId('model-templates-table')
    expect(table).toBeInTheDocument()
    expect(within(table).getByText('error <*> occurred')).toBeInTheDocument()
  })

  it('search box filters template rows by template_str', () => {
    mockUseModelsList.mockReturnValue({
      data: {
        models: [
          {
            model_key: 'docker:nginx',
            template_count: 2,
            line_count: 20,
            last_processed_ts: 1000,
            updated_at: 2000,
          },
        ],
      },
      isLoading: false,
      isSuccess: true,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useModelsList>)

    mockUseModelDetail.mockReturnValue({
      data: {
        model_key: 'docker:nginx',
        summary: {
          model_key: 'docker:nginx',
          template_count: 2,
          line_count: 20,
          last_processed_ts: 1000,
          updated_at: 2000,
        },
        templates: [
          {
            template_id: 1,
            template_hash: 'aaa',
            template_str: 'error <*> in nginx',
            size: 3,
            first_seen_ts: 1_700_000_000_000,
            last_seen_ts: 1_700_001_000_000,
          },
          {
            template_id: 2,
            template_hash: 'bbb',
            template_str: 'connection timeout',
            size: 2,
            first_seen_ts: 1_700_000_000_000,
            last_seen_ts: 1_700_001_000_000,
          },
        ],
      },
      isLoading: false,
      isSuccess: true,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useModelDetail>)

    render(<ModelsDebugPage />)

    // Click the model to show detail panel
    fireEvent.click(screen.getByTestId('model-list-item'))

    // Both templates are shown initially (scope to the desktop table — the
    // mobile cards render the same strings into the jsdom tree simultaneously)
    const table = screen.getByTestId('model-templates-table')
    expect(within(table).getByText('error <*> in nginx')).toBeInTheDocument()
    expect(within(table).getByText('connection timeout')).toBeInTheDocument()

    // Filter to only "error" (desktop search input — index 0; mobile shares state)
    const [searchInput] = screen.getAllByTestId('model-search')
    expect(searchInput).toBeDefined()
    fireEvent.change(searchInput as HTMLElement, { target: { value: 'error' } })

    expect(within(table).getByText('error <*> in nginx')).toBeInTheDocument()
    expect(within(table).queryByText('connection timeout')).not.toBeInTheDocument()
  })

  it('renders drain-disabled state when useModelsList returns an error', () => {
    mockUseModelsList.mockReturnValue({
      data: undefined,
      isLoading: false,
      isSuccess: false,
      isError: true,
      error: { status: 503, message: 'drain disabled' } as unknown as Error,
    } as unknown as ReturnType<typeof useModelsList>)

    render(<ModelsDebugPage />)

    expect(screen.getByText(/Drain disabled/)).toBeInTheDocument()
  })

  it('renders count-mismatch banner when stored count differs from live count', () => {
    mockUseModelsList.mockReturnValue({
      data: {
        models: [
          {
            model_key: 'docker:nginx',
            template_count: 5,
            line_count: 50,
            last_processed_ts: 1000,
            updated_at: 2000,
          },
        ],
      },
      isLoading: false,
      isSuccess: true,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useModelsList>)

    // Summary says 5 templates but only 1 in live
    mockUseModelDetail.mockReturnValue({
      data: {
        model_key: 'docker:nginx',
        summary: {
          model_key: 'docker:nginx',
          template_count: 5,
          line_count: 50,
          last_processed_ts: 1000,
          updated_at: 2000,
        },
        templates: [
          {
            template_id: 1,
            template_hash: 'aaa',
            template_str: 'only one template',
            size: 2,
            first_seen_ts: 1_700_000_000_000,
            last_seen_ts: 1_700_001_000_000,
          },
        ],
      },
      isLoading: false,
      isSuccess: true,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useModelDetail>)

    render(<ModelsDebugPage />)
    fireEvent.click(screen.getByTestId('model-list-item'))

    expect(screen.getAllByText(/Stored count 5 differs from live count 1/)[0]).toBeInTheDocument()
  })

  it('renders an Open-in-Explorer button for each template with non-generic template_str', () => {
    mockUseModelsList.mockReturnValue({
      data: {
        models: [
          {
            model_key: 'docker:nginx',
            template_count: 1,
            line_count: 10,
            last_processed_ts: 1000,
            updated_at: 2000,
          },
        ],
      },
      isLoading: false,
      isSuccess: true,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useModelsList>)

    mockUseModelDetail.mockReturnValue({
      data: {
        model_key: 'docker:nginx',
        summary: {
          model_key: 'docker:nginx',
          template_count: 1,
          line_count: 10,
          last_processed_ts: 1000,
          updated_at: 2000,
        },
        templates: [
          {
            template_id: 42,
            template_hash: 'abc12345',
            template_str: 'error <*> occurred',
            size: 3,
            first_seen_ts: 1_700_000_000_000,
            last_seen_ts: 1_700_001_000_000,
          },
        ],
      },
      isLoading: false,
      isSuccess: true,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useModelDetail>)

    render(<ModelsDebugPage />)
    fireEvent.click(screen.getByTestId('model-list-item'))

    // At least one Open-in-Explorer button visible (desktop table; mobile may also render)
    const buttons = screen.getAllByTestId('open-in-explorer')
    expect(buttons.length).toBeGreaterThan(0)
  })

  it('renders a View signature button for each template row', () => {
    mockUseModelsList.mockReturnValue({
      data: {
        models: [
          {
            model_key: 'docker:nginx',
            template_count: 1,
            line_count: 10,
            last_processed_ts: 1000,
            updated_at: 2000,
          },
        ],
      },
      isLoading: false,
      isSuccess: true,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useModelsList>)

    mockUseModelDetail.mockReturnValue({
      data: {
        model_key: 'docker:nginx',
        summary: {
          model_key: 'docker:nginx',
          template_count: 1,
          line_count: 10,
          last_processed_ts: 1000,
          updated_at: 2000,
        },
        templates: [
          {
            template_id: 42,
            template_hash: 'abc12345',
            template_str: 'error <*> occurred',
            size: 3,
            first_seen_ts: 1_700_000_000_000,
            last_seen_ts: 1_700_001_000_000,
          },
        ],
      },
      isLoading: false,
      isSuccess: true,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useModelDetail>)

    render(<ModelsDebugPage />)
    fireEvent.click(screen.getByTestId('model-list-item'))

    const viewSigButtons = screen.getAllByTestId('view-signature-link')
    expect(viewSigButtons.length).toBeGreaterThan(0)
  })

  it('clicking View signature navigates to the correct signature detail route', () => {
    mockUseModelsList.mockReturnValue({
      data: {
        models: [
          {
            model_key: 'docker:nginx',
            template_count: 1,
            line_count: 10,
            last_processed_ts: 1000,
            updated_at: 2000,
          },
        ],
      },
      isLoading: false,
      isSuccess: true,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useModelsList>)

    mockUseModelDetail.mockReturnValue({
      data: {
        model_key: 'docker:nginx',
        summary: {
          model_key: 'docker:nginx',
          template_count: 1,
          line_count: 10,
          last_processed_ts: 1000,
          updated_at: 2000,
        },
        templates: [
          {
            template_id: 42,
            template_hash: 'abc12345',
            template_str: 'error <*> occurred',
            size: 3,
            first_seen_ts: 1_700_000_000_000,
            last_seen_ts: 1_700_001_000_000,
          },
        ],
      },
      isLoading: false,
      isSuccess: true,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useModelDetail>)

    render(<ModelsDebugPage />)
    fireEvent.click(screen.getByTestId('model-list-item'))

    // Click first view-signature button (desktop row)
    fireEvent.click(screen.getAllByTestId('view-signature-link')[0]!)

    expect(mockNavigate).toHaveBeenCalledWith(
      expect.objectContaining({
        to: '/logs/signatures/$templateHash/$serviceKey',
        params: { templateHash: 'abc12345', serviceKey: 'docker:nginx' },
      }),
    )
  })

  it('shows stats caption when a model is selected', () => {
    mockUseModelsList.mockReturnValue({
      data: {
        models: [
          {
            model_key: 'docker:nginx',
            template_count: 1,
            line_count: 10,
            last_processed_ts: 1000,
            updated_at: 2000,
          },
        ],
      },
      isLoading: false,
      isSuccess: true,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useModelsList>)

    mockUseModelDetail.mockReturnValue({
      data: {
        model_key: 'docker:nginx',
        summary: {
          model_key: 'docker:nginx',
          template_count: 1,
          line_count: 10,
          last_processed_ts: 1000,
          updated_at: 2000,
        },
        templates: [
          {
            template_id: 42,
            template_hash: 'abc12345',
            template_str: 'error <*> occurred',
            size: 3,
            first_seen_ts: 1_700_000_000_000,
            last_seen_ts: 1_700_001_000_000,
          },
        ],
      },
      isLoading: false,
      isSuccess: true,
      isError: false,
      error: null,
    } as unknown as ReturnType<typeof useModelDetail>)

    render(<ModelsDebugPage />)
    fireEvent.click(screen.getByTestId('model-list-item'))

    const captions = screen.getAllByTestId('models-stats-caption')
    expect(captions.length).toBeGreaterThan(0)
    expect(captions[0]).toHaveTextContent('Counts reflect log lines ingested by drain')
  })
})
