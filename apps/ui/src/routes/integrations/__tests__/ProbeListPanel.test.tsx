import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ProbeListPanel } from '@/routes/integrations/ProbeListPanel'
import type { Schema } from '@/api/types'

afterEach(cleanup)

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

vi.mock('@/api/docker', () => ({
  useToggleProbe: vi.fn(() => ({
    mutate: vi.fn(),
    isPending: false,
  })),
  dockerProbeQueryKeys: {
    list: (name: string) => ['integrations', 'docker', 'containers', name, 'probes'],
  },
}))

vi.mock('@/lib/relativeTime', () => ({
  formatRelative: (timestamp: string) => {
    if (!timestamp) return '—'
    return `${new Date(timestamp).toLocaleDateString()}`
  },
  formatAbsolute: (timestamp: string) => {
    if (!timestamp) return '—'
    return new Date(timestamp).toISOString()
  },
}))

vi.mock('@/lib/useNowTick', () => {
  const FROZEN_NOW = 1_700_000_000_000
  return {
    useNowTick: () => FROZEN_NOW,
  }
})

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

type ProbeRow = Schema<'ProbeRow'>

function makeProbe(overrides: Partial<ProbeRow> = {}): ProbeRow {
  return {
    id: 'probe-001',
    container_name: 'homeassistant',
    kind: 'http',
    name: 'status',
    target_value: 'http://homeassistant:8123/api/',
    config_source: 'label',
    exec_authorized: false,
    enabled: true,
    last_status: 'ok',
    last_error: null,
    last_run_at: '2026-05-22T12:00:00Z',
    created_at: '2026-05-01T00:00:00Z',
    interval_seconds: 30,
    timeout_seconds: 10,
    ...overrides,
  }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('ProbeListPanel', () => {
  it('renders empty list when probes is empty', () => {
    render(<ProbeListPanel probes={[]} />)
    // No rows should be rendered
    expect(screen.queryByText('status')).not.toBeInTheDocument()
  })

  it('renders Source column with config_source value for each probe', () => {
    const probes: ProbeRow[] = [
      makeProbe({ name: 'probe1', config_source: 'label' }),
      makeProbe({ name: 'probe2', config_source: 'file_override' }),
      makeProbe({ name: 'probe3', config_source: 'auto_default' }),
    ]
    render(<ProbeListPanel probes={probes} />)

    // Desktop table should have Source column header
    expect(screen.getByText('Source')).toBeInTheDocument()

    // Each probe should render its config_source
    expect(screen.getAllByText('Label').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Config file').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Auto-detect').length).toBeGreaterThan(0)
  })

  it('renders source label in mobile card view', () => {
    const probes: ProbeRow[] = [makeProbe({ name: 'check1', config_source: 'label' })]
    render(<ProbeListPanel probes={probes} />)

    // Mobile view should have "Source:" label
    const sourceText = screen.getByText(/Source:/)
    expect(sourceText).toBeInTheDocument()
    expect(sourceText).toHaveTextContent('Label')
  })

  it('renders source badges for all source types distinctly', () => {
    const probes: ProbeRow[] = [
      makeProbe({ id: 'p1', name: 'p1', config_source: 'label' }),
      makeProbe({ id: 'p2', name: 'p2', config_source: 'file_override' }),
      makeProbe({ id: 'p3', name: 'p3', config_source: 'auto_default' }),
      makeProbe({ id: 'p4', name: 'p4', config_source: 'discovered_accepted' }),
    ]
    render(<ProbeListPanel probes={probes} />)

    // All sources should be present and distinct
    expect(screen.getAllByText('Label').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Config file').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Auto-detect').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Accepted').length).toBeGreaterThan(0)
  })

  it('renders Kind and Name columns correctly', () => {
    const probes: ProbeRow[] = [
      makeProbe({ kind: 'http', name: 'health' }),
      makeProbe({ id: 'p2', kind: 'exec', name: 'backup' }),
    ]
    render(<ProbeListPanel probes={probes} />)

    expect(screen.getAllByText('http').length).toBeGreaterThan(0)
    expect(screen.getAllByText('exec').length).toBeGreaterThan(0)
    expect(screen.getAllByText('health').length).toBeGreaterThan(0)
    expect(screen.getAllByText('backup').length).toBeGreaterThan(0)
  })

  it('renders Status column with status badge', () => {
    const probes: ProbeRow[] = [
      makeProbe({ last_status: 'ok' }),
      makeProbe({ id: 'p2', last_status: 'fail' }),
    ]
    render(<ProbeListPanel probes={probes} />)

    expect(screen.getAllByText('OK').length).toBeGreaterThan(0)
    expect(screen.getAllByText('FAILING').length).toBeGreaterThan(0)
  })

  it('renders Target column with truncated values', () => {
    const longTarget = 'http://example.com/very/long/path/that/should/be/truncated'
    const probes: ProbeRow[] = [makeProbe({ target_value: longTarget })]
    render(<ProbeListPanel probes={probes} />)

    const targetCell = screen.getAllByTitle(longTarget)[0]
    expect(targetCell).toBeInTheDocument()
  })

  it('renders enabled/disabled button per probe', () => {
    const probes: ProbeRow[] = [
      makeProbe({ enabled: true }),
      makeProbe({ id: 'p2', enabled: false }),
    ]
    render(<ProbeListPanel probes={probes} />)

    const buttons = screen.getAllByRole('button')
    // Should have at least 2 buttons (one per probe)
    expect(buttons.length).toBeGreaterThanOrEqual(2)
    expect(screen.getAllByText('Disable').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Enable').length).toBeGreaterThan(0)
  })

  it('renders mobile card with Source badge inline', () => {
    const probes: ProbeRow[] = [makeProbe({ name: 'mobile-test', config_source: 'label' })]
    render(<ProbeListPanel probes={probes} />)

    // The Source: label should be present
    const source = screen.getByText(/Source:/)
    expect(source).toBeInTheDocument()

    // The source value should be in a badge
    const badge = source.textContent?.includes('Label')
    expect(badge).toBeTruthy()
  })

  it('renders multiple probes in both desktop and mobile views', () => {
    const probes: ProbeRow[] = [
      makeProbe({ id: 'p1', name: 'probe1', config_source: 'label' }),
      makeProbe({ id: 'p2', name: 'probe2', config_source: 'file_override' }),
      makeProbe({ id: 'p3', name: 'probe3', config_source: 'auto_default' }),
    ]
    render(<ProbeListPanel probes={probes} />)

    // All probes should be rendered
    expect(screen.getAllByText('probe1').length).toBeGreaterThan(0)
    expect(screen.getAllByText('probe2').length).toBeGreaterThan(0)
    expect(screen.getAllByText('probe3').length).toBeGreaterThan(0)

    // All sources should be rendered
    expect(screen.getAllByText('Label').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Config file').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Auto-detect').length).toBeGreaterThan(0)
  })

  it('renders probe with error message', () => {
    const probes: ProbeRow[] = [makeProbe({ last_error: 'Connection timeout' })]
    render(<ProbeListPanel probes={probes} />)

    expect(screen.getAllByText('Connection timeout').length).toBeGreaterThan(0)
  })

  it('omits error message when last_error is null', () => {
    const probes: ProbeRow[] = [makeProbe({ last_error: null })]
    render(<ProbeListPanel probes={probes} />)

    // Should not have "Error:" label when last_error is null
    expect(screen.queryByText(/^Error:/)).not.toBeInTheDocument()
  })
})
