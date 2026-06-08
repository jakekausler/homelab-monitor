import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/signatures', () => ({
  useSignaturesQuery: vi.fn(),
}))

vi.mock('@/api/models', () => ({
  useLastCycle: vi.fn(() => ({ data: { has_run: false }, isLoading: false })),
  useTriggerRefresh: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
}))

const navigateSpy = vi.fn()
vi.mock('@tanstack/react-router', async () => {
  const actual =
    await vi.importActual<typeof import('@tanstack/react-router')>('@tanstack/react-router')
  return {
    ...actual,
    useSearch: vi.fn(() => ({ service: undefined, status: undefined, label_q: undefined })),
    useNavigate: vi.fn(() => navigateSpy),
  }
})

import { useSearch } from '@tanstack/react-router'

import { useSignaturesQuery } from '@/api/signatures'
import { SignaturesTab } from '../SignaturesTab'

const mockUseSignaturesQuery = vi.mocked(useSignaturesQuery)
const mockUseSearch = vi.mocked(useSearch)

afterEach(() => {
  cleanup()
})

describe('SignaturesTab', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders empty state when no signatures exist', () => {
    mockUseSignaturesQuery.mockReturnValue({
      data: { signatures: [], total: 0 },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useSignaturesQuery>)

    render(<SignaturesTab />)
    expect(
      screen.getByText(/No signatures yet — they appear after the drain consumer runs/),
    ).toBeInTheDocument()
  })

  it('renders table with signatures', () => {
    const mockSigs = [
      {
        template_hash: 'hash1',
        service_key: 'docker:nginx',
        template_str: 'error <*> occurred',
        label: 'test-label',
        status: 'active' as const,
        first_seen_at: 1000,
        last_seen_at: 2000,
        total_count: 42,
      },
    ]
    mockUseSignaturesQuery.mockReturnValue({
      data: { signatures: mockSigs, total: 1 },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useSignaturesQuery>)

    render(<SignaturesTab />)
    expect(screen.getByTestId('signatures-table')).toBeInTheDocument()
    expect(screen.getByTestId('signature-row')).toBeInTheDocument()
    expect(screen.getAllByText('docker:nginx').length).toBeGreaterThan(0)
    expect(screen.getAllByText('test-label').length).toBeGreaterThan(0)
    expect(screen.getAllByText('active').length).toBeGreaterThan(0)
  })

  it('renders row with data attributes for testing', () => {
    const mockSigs = [
      {
        template_hash: 'hash-abc',
        service_key: 'docker:nginx',
        template_str: 'test template',
        label: null,
        status: 'active' as const,
        first_seen_at: 1000,
        last_seen_at: 2000,
        total_count: 10,
      },
    ]
    mockUseSignaturesQuery.mockReturnValue({
      data: { signatures: mockSigs, total: 1 },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useSignaturesQuery>)

    render(<SignaturesTab />)
    const row = screen.getByTestId('signature-row')
    expect(row).toHaveAttribute('data-template-hash', 'hash-abc')
    expect(row).toHaveAttribute('data-service-key', 'docker:nginx')
  })

  it('renders status badge', () => {
    const mockSigs = [
      {
        template_hash: 'hash1',
        service_key: 'docker:nginx',
        template_str: 'error',
        label: null,
        status: 'suppressed' as const,
        first_seen_at: 1000,
        last_seen_at: 2000,
        total_count: 5,
      },
    ]
    mockUseSignaturesQuery.mockReturnValue({
      data: { signatures: mockSigs, total: 1 },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useSignaturesQuery>)

    render(<SignaturesTab />)
    expect(screen.getAllByText('suppressed')[0]).toBeInTheDocument()
  })

  it('renders filter inputs', () => {
    mockUseSignaturesQuery.mockReturnValue({
      data: { signatures: [], total: 0 },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useSignaturesQuery>)

    render(<SignaturesTab />)
    expect(screen.getByPlaceholderText('Filter by service...')).toBeInTheDocument()
    expect(screen.getByPlaceholderText('Filter by label...')).toBeInTheDocument()
    expect(screen.getByRole('combobox')).toBeInTheDocument()
  })

  it('displays label as — when null', () => {
    const mockSigs = [
      {
        template_hash: 'hash1',
        service_key: 'docker:nginx',
        template_str: 'error',
        label: null,
        status: 'active' as const,
        first_seen_at: 1000,
        last_seen_at: 2000,
        total_count: 5,
      },
    ]
    mockUseSignaturesQuery.mockReturnValue({
      data: { signatures: mockSigs, total: 1 },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useSignaturesQuery>)

    render(<SignaturesTab />)
    expect(screen.getAllByText('—')[0]).toBeInTheDocument()
  })

  it('renders loading state', () => {
    mockUseSignaturesQuery.mockReturnValue({
      data: undefined,
      isLoading: true,
      error: null,
    } as unknown as ReturnType<typeof useSignaturesQuery>)

    render(<SignaturesTab />)
    expect(screen.getByText('Loading signatures...')).toBeInTheDocument()
  })

  it('renders error message on query failure', () => {
    mockUseSignaturesQuery.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: new Error('api error'),
    } as unknown as ReturnType<typeof useSignaturesQuery>)

    render(<SignaturesTab />)
    expect(screen.getByText('Error loading signatures')).toBeInTheDocument()
  })

  it('renders mobile cards alongside the desktop table', () => {
    const mockSigs = [
      {
        template_hash: 'hash-abc',
        service_key: 'docker:nginx',
        template_str: 'test template',
        label: null,
        status: 'active' as const,
        first_seen_at: 1000,
        last_seen_at: 2000,
        total_count: 10,
      },
    ]
    mockUseSignaturesQuery.mockReturnValue({
      data: { signatures: mockSigs, total: 1 },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useSignaturesQuery>)

    render(<SignaturesTab />)
    const card = screen.getByTestId('signature-card')
    expect(card).toHaveAttribute('data-template-hash', 'hash-abc')
    expect(card).toHaveAttribute('data-service-key', 'docker:nginx')
  })

  it('navigates to the detail route on row click', () => {
    const mockSigs = [
      {
        template_hash: 'hash-abc',
        service_key: 'docker:nginx',
        template_str: 'test template',
        label: null,
        status: 'active' as const,
        first_seen_at: 1000,
        last_seen_at: 2000,
        total_count: 10,
      },
    ]
    mockUseSignaturesQuery.mockReturnValue({
      data: { signatures: mockSigs, total: 1 },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useSignaturesQuery>)

    render(<SignaturesTab />)
    fireEvent.click(screen.getByTestId('signature-row'))
    expect(navigateSpy).toHaveBeenCalledWith({
      to: '/logs/signatures/$templateHash/$serviceKey',
      params: { templateHash: 'hash-abc', serviceKey: 'docker:nginx' },
    })
  })

  it('clearing a set service filter navigates with the service key omitted', () => {
    // A filter is currently set in the URL.
    mockUseSearch.mockReturnValue({ service: 'nginx', status: undefined, label_q: undefined })
    mockUseSignaturesQuery.mockReturnValue({
      data: { signatures: [], total: 0 },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useSignaturesQuery>)

    render(<SignaturesTab />)
    // Clear the service input.
    fireEvent.change(screen.getByPlaceholderText('Filter by service...'), {
      target: { value: '' },
    })
    // The navigate search must OMIT the cleared service key (not retain the old value).
    expect(navigateSpy).toHaveBeenCalledWith({ to: '/logs/signatures', search: {} })
  })

  it('renders the signatures description block', () => {
    mockUseSignaturesQuery.mockReturnValue({
      data: { signatures: [], total: 0 },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useSignaturesQuery>)

    render(<SignaturesTab />)
    expect(screen.getByTestId('signatures-description')).toBeInTheDocument()
  })
})
