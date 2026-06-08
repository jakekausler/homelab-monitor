import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import userEvent from '@testing-library/user-event'

vi.mock('@/api/silenceAllowlist', () => ({
  useSilenceAllowlist: vi.fn(),
  useCreateSilenceAllowlistEntry: vi.fn(),
  useDeleteSilenceAllowlistEntry: vi.fn(),
}))

vi.mock('@/api/signatures', () => ({
  useSignaturesQuery: vi.fn(),
}))

import {
  useSilenceAllowlist,
  useCreateSilenceAllowlistEntry,
  useDeleteSilenceAllowlistEntry,
} from '@/api/silenceAllowlist'
import { useSignaturesQuery } from '@/api/signatures'
import { SilenceAllowlistTab } from '../SilenceAllowlistTab'

const mockUseSilenceAllowlist = vi.mocked(useSilenceAllowlist)
const mockUseCreateSilenceAllowlistEntry = vi.mocked(useCreateSilenceAllowlistEntry)
const mockUseDeleteSilenceAllowlistEntry = vi.mocked(useDeleteSilenceAllowlistEntry)
const mockUseSignaturesQuery = vi.mocked(useSignaturesQuery)

const mockSignaturesQueryEmpty = {
  data: { signatures: [], total: 0 },
  isLoading: false,
  isError: false,
} as unknown as ReturnType<typeof useSignaturesQuery>

afterEach(() => {
  cleanup()
})

describe('SilenceAllowlistTab', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockUseSignaturesQuery.mockReturnValue(mockSignaturesQueryEmpty)
  })

  it('renders empty state when no entries exist', () => {
    mockUseSilenceAllowlist.mockReturnValue({
      data: { entries: [] },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useSilenceAllowlist>)
    mockUseCreateSilenceAllowlistEntry.mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
      error: null,
    } as unknown as ReturnType<typeof useCreateSilenceAllowlistEntry>)
    mockUseDeleteSilenceAllowlistEntry.mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
      error: null,
    } as unknown as ReturnType<typeof useDeleteSilenceAllowlistEntry>)

    render(<SilenceAllowlistTab />)
    expect(screen.getByText('No silence allowlist entries yet.')).toBeInTheDocument()
  })

  it('renders form and table sections', () => {
    mockUseSilenceAllowlist.mockReturnValue({
      data: { entries: [] },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useSilenceAllowlist>)
    mockUseCreateSilenceAllowlistEntry.mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
      error: null,
    } as unknown as ReturnType<typeof useCreateSilenceAllowlistEntry>)
    mockUseDeleteSilenceAllowlistEntry.mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
      error: null,
    } as unknown as ReturnType<typeof useDeleteSilenceAllowlistEntry>)

    render(<SilenceAllowlistTab />)
    expect(screen.getByRole('button', { name: 'Create Entry' })).toBeInTheDocument()
    expect(screen.getByText('Entries')).toBeInTheDocument()
    expect(screen.getByTestId('silence-service-key')).toBeInTheDocument()
    expect(screen.getByTestId('silence-schedule-kind')).toBeInTheDocument()
    expect(screen.getByTestId('silence-reason')).toBeInTheDocument()
  })

  it('renders form fields with correct labels', () => {
    mockUseSilenceAllowlist.mockReturnValue({
      data: { entries: [] },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useSilenceAllowlist>)
    mockUseCreateSilenceAllowlistEntry.mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
      error: null,
    } as unknown as ReturnType<typeof useCreateSilenceAllowlistEntry>)
    mockUseDeleteSilenceAllowlistEntry.mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
      error: null,
    } as unknown as ReturnType<typeof useDeleteSilenceAllowlistEntry>)

    render(<SilenceAllowlistTab />)
    expect(screen.getByLabelText('Service Key *')).toBeInTheDocument()
    expect(screen.getByLabelText('Signature (optional)')).toBeInTheDocument()
    expect(screen.getByLabelText('Schedule Type *')).toBeInTheDocument()
    expect(screen.getByLabelText('Reason *')).toBeInTheDocument()
  })

  it('hides schedule_value for always kind', () => {
    mockUseSilenceAllowlist.mockReturnValue({
      data: { entries: [] },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useSilenceAllowlist>)
    mockUseCreateSilenceAllowlistEntry.mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
      error: null,
    } as unknown as ReturnType<typeof useCreateSilenceAllowlistEntry>)
    mockUseDeleteSilenceAllowlistEntry.mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
      error: null,
    } as unknown as ReturnType<typeof useDeleteSilenceAllowlistEntry>)

    render(<SilenceAllowlistTab />)
    const kindSelect = screen.getByTestId<HTMLSelectElement>('silence-schedule-kind')
    expect(kindSelect.value).toBe('always')

    const cronValue = screen.queryByTestId('silence-cron-value')
    expect(cronValue).not.toBeInTheDocument()

    const windowStart = screen.queryByTestId('silence-window-start')
    expect(windowStart).not.toBeInTheDocument()
  })

  it('shows cron field when schedule_kind changes to cron', () => {
    mockUseSilenceAllowlist.mockReturnValue({
      data: { entries: [] },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useSilenceAllowlist>)
    mockUseCreateSilenceAllowlistEntry.mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
      error: null,
    } as unknown as ReturnType<typeof useCreateSilenceAllowlistEntry>)
    mockUseDeleteSilenceAllowlistEntry.mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
      error: null,
    } as unknown as ReturnType<typeof useDeleteSilenceAllowlistEntry>)

    render(<SilenceAllowlistTab />)
    const kindSelect = screen.getByTestId('silence-schedule-kind')

    fireEvent.change(kindSelect, { target: { value: 'cron' } })

    expect(screen.getByTestId('silence-cron-value')).toBeInTheDocument()
    expect(screen.queryByTestId('silence-window-start')).not.toBeInTheDocument()
  })

  it('shows window fields when schedule_kind changes to window', () => {
    mockUseSilenceAllowlist.mockReturnValue({
      data: { entries: [] },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useSilenceAllowlist>)
    mockUseCreateSilenceAllowlistEntry.mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
      error: null,
    } as unknown as ReturnType<typeof useCreateSilenceAllowlistEntry>)
    mockUseDeleteSilenceAllowlistEntry.mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
      error: null,
    } as unknown as ReturnType<typeof useDeleteSilenceAllowlistEntry>)

    render(<SilenceAllowlistTab />)
    const kindSelect = screen.getByTestId('silence-schedule-kind')

    fireEvent.change(kindSelect, { target: { value: 'window' } })

    expect(screen.getByTestId('silence-window-start')).toBeInTheDocument()
    expect(screen.getByTestId('silence-window-end')).toBeInTheDocument()
    expect(screen.queryByTestId('silence-cron-value')).not.toBeInTheDocument()
  })

  it('renders list of entries when data exists', () => {
    const mockEntries = [
      {
        id: 1,
        template_hash: 'h1',
        service_key: 'svc1',
        schedule_kind: 'always' as const,
        schedule_value: '',
        reason: 'test entry 1',
        created_at: '2026-01-01T00:00:00+00:00',
        expires_at: null,
      },
      {
        id: 2,
        template_hash: null,
        service_key: 'svc2',
        schedule_kind: 'cron' as const,
        schedule_value: '0 * * * *',
        reason: 'test entry 2',
        created_at: '2026-01-02T00:00:00+00:00',
        expires_at: '2026-12-31T00:00:00+00:00',
      },
    ]

    mockUseSilenceAllowlist.mockReturnValue({
      data: { entries: mockEntries },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useSilenceAllowlist>)
    mockUseCreateSilenceAllowlistEntry.mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
      error: null,
    } as unknown as ReturnType<typeof useCreateSilenceAllowlistEntry>)
    mockUseDeleteSilenceAllowlistEntry.mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
      error: null,
    } as unknown as ReturnType<typeof useDeleteSilenceAllowlistEntry>)

    render(<SilenceAllowlistTab />)

    const rows = within(screen.getByTestId('silence-allowlist-table')).getAllByTestId(
      'silence-allowlist-row',
    )
    expect(rows).toHaveLength(2)

    const row1 = rows[0]
    expect(within(row1 as HTMLElement).getByText('svc1')).toBeInTheDocument()
    expect(within(row1 as HTMLElement).getByText('always')).toBeInTheDocument()

    const row2 = rows[1]
    expect(within(row2 as HTMLElement).getByText('svc2')).toBeInTheDocument()
    expect(within(row2 as HTMLElement).getByText('cron')).toBeInTheDocument()
  })

  it('calls delete mutation when delete button clicked', () => {
    const deleteSpyFn = vi.fn()
    const mockEntries = [
      {
        id: 123,
        template_hash: 'h1',
        service_key: 'svc1',
        schedule_kind: 'always' as const,
        schedule_value: '',
        reason: 'test',
        created_at: '2026-01-01T00:00:00+00:00',
        expires_at: null,
      },
    ]

    mockUseSilenceAllowlist.mockReturnValue({
      data: { entries: mockEntries },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useSilenceAllowlist>)
    mockUseCreateSilenceAllowlistEntry.mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
      error: null,
    } as unknown as ReturnType<typeof useCreateSilenceAllowlistEntry>)
    mockUseDeleteSilenceAllowlistEntry.mockReturnValue({
      mutate: deleteSpyFn,
      isPending: false,
      error: null,
    } as unknown as ReturnType<typeof useDeleteSilenceAllowlistEntry>)

    render(<SilenceAllowlistTab />)

    const deleteButton = screen.getAllByTestId('silence-delete')[0] as HTMLElement
    fireEvent.click(deleteButton)

    expect(deleteSpyFn).toHaveBeenCalledWith(123)
  })

  it('renders table with all columns on desktop', () => {
    const mockEntries = [
      {
        id: 1,
        template_hash: 'h1',
        service_key: 'svc1',
        schedule_kind: 'cron' as const,
        schedule_value: '0 * * * *',
        reason: 'test reason',
        created_at: '2026-01-01T00:00:00+00:00',
        expires_at: '2026-12-31T00:00:00+00:00',
      },
    ]

    mockUseSilenceAllowlist.mockReturnValue({
      data: { entries: mockEntries },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useSilenceAllowlist>)
    mockUseCreateSilenceAllowlistEntry.mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
      error: null,
    } as unknown as ReturnType<typeof useCreateSilenceAllowlistEntry>)
    mockUseDeleteSilenceAllowlistEntry.mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
      error: null,
    } as unknown as ReturnType<typeof useDeleteSilenceAllowlistEntry>)

    render(<SilenceAllowlistTab />)

    const table = screen.getByTestId('silence-allowlist-table')
    expect(table).toBeInTheDocument()

    const row = within(table).getByTestId('silence-allowlist-row')
    expect(within(row).getByText('svc1')).toBeInTheDocument()
    expect(within(row).getByText('h1')).toBeInTheDocument()
    expect(within(row).getByText('cron')).toBeInTheDocument()
    expect(within(row).getByText('0 * * * *')).toBeInTheDocument()
    expect(within(row).getByText('test reason')).toBeInTheDocument()
  })

  it('renders per-service entry without template_hash', () => {
    const mockEntries = [
      {
        id: 1,
        template_hash: null,
        service_key: 'svc1',
        schedule_kind: 'always' as const,
        schedule_value: '',
        reason: 'applies to all',
        created_at: '2026-01-01T00:00:00+00:00',
        expires_at: null,
      },
    ]

    mockUseSilenceAllowlist.mockReturnValue({
      data: { entries: mockEntries },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useSilenceAllowlist>)
    mockUseCreateSilenceAllowlistEntry.mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
      error: null,
    } as unknown as ReturnType<typeof useCreateSilenceAllowlistEntry>)
    mockUseDeleteSilenceAllowlistEntry.mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
      error: null,
    } as unknown as ReturnType<typeof useDeleteSilenceAllowlistEntry>)

    render(<SilenceAllowlistTab />)

    expect(screen.getByText('(all signatures)')).toBeInTheDocument()
  })

  it('shows loading state', () => {
    mockUseSilenceAllowlist.mockReturnValue({
      data: undefined,
      isLoading: true,
      error: null,
    } as unknown as ReturnType<typeof useSilenceAllowlist>)
    mockUseCreateSilenceAllowlistEntry.mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
      error: null,
    } as unknown as ReturnType<typeof useCreateSilenceAllowlistEntry>)
    mockUseDeleteSilenceAllowlistEntry.mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
      error: null,
    } as unknown as ReturnType<typeof useDeleteSilenceAllowlistEntry>)

    render(<SilenceAllowlistTab />)
    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('shows error message on query failure', () => {
    mockUseSilenceAllowlist.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: new Error('api error'),
    } as unknown as ReturnType<typeof useSilenceAllowlist>)
    mockUseCreateSilenceAllowlistEntry.mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
      error: null,
    } as unknown as ReturnType<typeof useCreateSilenceAllowlistEntry>)
    mockUseDeleteSilenceAllowlistEntry.mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
      error: null,
    } as unknown as ReturnType<typeof useDeleteSilenceAllowlistEntry>)

    render(<SilenceAllowlistTab />)
    expect(screen.getByText('Error loading silence allowlist')).toBeInTheDocument()
  })

  it('renders the description block', () => {
    mockUseSilenceAllowlist.mockReturnValue({
      data: { entries: [] },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useSilenceAllowlist>)
    mockUseCreateSilenceAllowlistEntry.mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
      error: null,
    } as unknown as ReturnType<typeof useCreateSilenceAllowlistEntry>)
    mockUseDeleteSilenceAllowlistEntry.mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
      error: null,
    } as unknown as ReturnType<typeof useDeleteSilenceAllowlistEntry>)

    render(<SilenceAllowlistTab />)
    const desc = screen.getByTestId('silence-allowlist-description')
    expect(desc).toBeInTheDocument()
    expect(within(desc).getByText('Silence Allowlist')).toBeInTheDocument()
  })

  it('labels the template_hash field as "Signature (optional)"', () => {
    mockUseSilenceAllowlist.mockReturnValue({
      data: { entries: [] },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useSilenceAllowlist>)
    mockUseCreateSilenceAllowlistEntry.mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
      error: null,
    } as unknown as ReturnType<typeof useCreateSilenceAllowlistEntry>)
    mockUseDeleteSilenceAllowlistEntry.mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
      error: null,
    } as unknown as ReturnType<typeof useDeleteSilenceAllowlistEntry>)

    render(<SilenceAllowlistTab />)
    expect(screen.getByText('Signature (optional)')).toBeInTheDocument()
    expect(screen.queryByText('Template Hash (optional)')).not.toBeInTheDocument()
  })

  it('renders the signature picker', () => {
    mockUseSilenceAllowlist.mockReturnValue({
      data: { entries: [] },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useSilenceAllowlist>)
    mockUseCreateSilenceAllowlistEntry.mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
      error: null,
    } as unknown as ReturnType<typeof useCreateSilenceAllowlistEntry>)
    mockUseDeleteSilenceAllowlistEntry.mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
      error: null,
    } as unknown as ReturnType<typeof useDeleteSilenceAllowlistEntry>)

    render(<SilenceAllowlistTab />)
    expect(screen.getByTestId('silence-signature-picker')).toBeInTheDocument()
  })

  it('autofills service_key and template_hash when a signature is selected', async () => {
    const mockSig = {
      template_hash: 'abc123',
      service_key: 'plex',
      template_str: 'Starting Plex Media Server',
      status: 'active' as const,
      first_seen_at: 0,
      last_seen_at: 0,
      total_count: 1,
      label: null,
    }
    mockUseSignaturesQuery.mockReturnValue({
      data: { signatures: [mockSig], total: 1 },
      isLoading: false,
      isError: false,
    } as unknown as ReturnType<typeof useSignaturesQuery>)

    mockUseSilenceAllowlist.mockReturnValue({
      data: { entries: [] },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useSilenceAllowlist>)
    mockUseCreateSilenceAllowlistEntry.mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
      error: null,
    } as unknown as ReturnType<typeof useCreateSilenceAllowlistEntry>)
    mockUseDeleteSilenceAllowlistEntry.mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
      error: null,
    } as unknown as ReturnType<typeof useDeleteSilenceAllowlistEntry>)

    render(<SilenceAllowlistTab />)

    const picker = screen.getByTestId('silence-signature-picker')
    await userEvent.selectOptions(picker, 'plex:abc123')

    const serviceKeyInput = screen.getByTestId('silence-service-key')
    const templateHashInput = screen.getByTestId('silence-template-hash')
    expect(serviceKeyInput).toHaveValue('plex')
    expect(templateHashInput).toHaveValue('abc123')
  })

  it('renders the picker with only the placeholder option when signatures list is empty', () => {
    mockUseSilenceAllowlist.mockReturnValue({
      data: { entries: [] },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useSilenceAllowlist>)
    mockUseCreateSilenceAllowlistEntry.mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
      error: null,
    } as unknown as ReturnType<typeof useCreateSilenceAllowlistEntry>)
    mockUseDeleteSilenceAllowlistEntry.mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
      error: null,
    } as unknown as ReturnType<typeof useDeleteSilenceAllowlistEntry>)

    render(<SilenceAllowlistTab />)
    const picker = screen.getByTestId<HTMLSelectElement>('silence-signature-picker')
    expect(picker.options).toHaveLength(1)
    expect(picker.options[0]?.text).toBe('— Select a signature to autofill —')
  })
})
