import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { TooltipProvider } from '@/components/ui/tooltip'
import { QueryHistoryPanel } from '@/routes/logs/QueryHistoryPanel'
import { recordQuery, type HistoryEntry } from '@/lib/queryHistory'

const makeEntry = (overrides?: Partial<HistoryEntry>): HistoryEntry => ({
  id: 'test-id',
  timestamp: Date.now(),
  advanced_mode: false,
  logs_ql: 'test query',
  selected_services: [{ service: 'svc1', source_type: 'docker' }],
  since_preset: '1h',
  ...overrides,
})

beforeEach(() => {
  window.localStorage.clear()
})

afterEach(() => {
  cleanup()
  window.localStorage.clear()
})

describe('QueryHistoryPanel', () => {
  it('renders empty state when no history is present', () => {
    const onLoad = vi.fn()
    render(
      <TooltipProvider>
        <QueryHistoryPanel onLoad={onLoad} />
      </TooltipProvider>,
    )
    expect(screen.getByTestId('logs-history-empty')).toBeInTheDocument()
    expect(screen.getByText(/No recent queries yet/i)).toBeInTheDocument()
  })

  it('renders rows in most-recent-first order', () => {
    const entry1 = makeEntry({
      id: 'id-1',
      timestamp: 1000,
      logs_ql: 'query 1',
      selected_services: [],
    })
    const entry2 = makeEntry({
      id: 'id-2',
      timestamp: 2000,
      logs_ql: 'query 2',
      selected_services: [],
    })
    recordQuery(entry1)
    recordQuery(entry2)

    const onLoad = vi.fn()
    render(
      <TooltipProvider>
        <QueryHistoryPanel onLoad={onLoad} />
      </TooltipProvider>,
    )

    const rows = screen.getAllByTestId('logs-history-row')
    expect(rows).toHaveLength(2)
    // Most recent first
    expect(rows[0]?.textContent).toContain('query 2')
    expect(rows[1]?.textContent).toContain('query 1')
  })

  it('renders relative time formatting', () => {
    const now = Date.now()
    const entry = makeEntry({
      timestamp: now - 60000, // 1 minute ago
      logs_ql: 'test query',
      selected_services: [],
    })
    recordQuery(entry)

    render(
      <TooltipProvider>
        <QueryHistoryPanel onLoad={vi.fn()} />
      </TooltipProvider>,
    )

    const row = screen.getByTestId('logs-history-row')
    expect(row.textContent).toMatch(/ago|just now/) // formatRelative output
  })

  it('renders the query preview with expression and services', () => {
    const entry = makeEntry({
      logs_ql: '_msg:"error"',
      selected_services: [
        { service: 'api', source_type: 'docker' },
        { service: 'db', source_type: 'docker' },
      ],
      since_preset: '1h',
    })
    recordQuery(entry)

    render(
      <TooltipProvider>
        <QueryHistoryPanel onLoad={vi.fn()} />
      </TooltipProvider>,
    )

    const row = screen.getByTestId('logs-history-row')
    expect(row.textContent).toContain('_msg:"error"')
    expect(row.textContent).toContain('docker:api')
    expect(row.textContent).toContain('docker:db')
    expect(row.textContent).toContain('1h')
  })

  it('renders (all) when advanced_mode is false and logs_ql is empty', () => {
    const entry = makeEntry({
      advanced_mode: false,
      logs_ql: '',
      selected_services: [],
    })
    recordQuery(entry)

    render(
      <TooltipProvider>
        <QueryHistoryPanel onLoad={vi.fn()} />
      </TooltipProvider>,
    )

    const row = screen.getByTestId('logs-history-row')
    expect(row.textContent).toContain('(all)')
  })

  it('renders * when advanced_mode is true and logs_ql is empty', () => {
    const entry = makeEntry({
      advanced_mode: true,
      logs_ql: '',
      selected_services: [],
    })
    recordQuery(entry)

    render(
      <TooltipProvider>
        <QueryHistoryPanel onLoad={vi.fn()} />
      </TooltipProvider>,
    )

    const row = screen.getByTestId('logs-history-row')
    expect(row.textContent).toContain('*')
  })

  it('calls onLoad with the entry when a row is clicked', async () => {
    const user = userEvent.setup()
    const entry = makeEntry({
      logs_ql: 'unique query',
      selected_services: [],
    })
    recordQuery(entry)

    const onLoad = vi.fn()
    render(
      <TooltipProvider>
        <QueryHistoryPanel onLoad={onLoad} />
      </TooltipProvider>,
    )

    const row = screen.getByTestId('logs-history-row')
    await user.click(row)

    expect(onLoad).toHaveBeenCalledWith(expect.objectContaining({ logs_ql: 'unique query' }))
  })

  it('clears history when the Clear button is clicked', async () => {
    const user = userEvent.setup()
    const entry = makeEntry({ logs_ql: 'query' })
    recordQuery(entry)

    render(
      <TooltipProvider>
        <QueryHistoryPanel onLoad={vi.fn()} />
      </TooltipProvider>,
    )

    expect(screen.queryByTestId('logs-history-row')).toBeInTheDocument()
    const clearBtn = screen.getByTestId('logs-history-clear')
    await user.click(clearBtn)

    // Wait for the empty state to appear
    await waitFor(() => {
      expect(screen.getByTestId('logs-history-empty')).toBeInTheDocument()
      expect(screen.queryByTestId('logs-history-row')).not.toBeInTheDocument()
    })
  })

  it('is reactive to pub-sub changes (no remount required)', async () => {
    const onLoad = vi.fn()
    render(
      <TooltipProvider>
        <QueryHistoryPanel onLoad={onLoad} />
      </TooltipProvider>,
    )

    // Should show empty initially
    expect(screen.getByTestId('logs-history-empty')).toBeInTheDocument()

    // Record an entry via the module API (same-tab pub-sub)
    const entry = makeEntry({ logs_ql: 'new query' })
    recordQuery(entry)

    // Wait for the panel to update without remounting
    await waitFor(() => {
      expect(screen.getByTestId('logs-history-row')).toBeInTheDocument()
      expect(screen.getByText(/new query/i)).toBeInTheDocument()
    })
  })
})
