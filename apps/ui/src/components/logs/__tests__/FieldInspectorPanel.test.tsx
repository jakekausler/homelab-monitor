import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { FieldInspectorPanel } from '../FieldInspectorPanel'
import type { LogLine } from '../types'

vi.mock('@/lib/useCopyToClipboard', () => ({
  useCopyToClipboard: () => vi.fn(),
}))

describe('FieldInspectorPanel', () => {
  const mockLine: LogLine = {
    timestamp: '2024-01-15T10:30:45.123Z',
    severity: 'info',
    service: 'nginx',
    host: 'host-1',
    stream: 'stdout',
    message: 'Request received',
    fields: {
      source_type: 'docker',
      severity_raw: 'INFO',
      zeta: 'z',
      alpha: 'a',
    },
  }

  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    cleanup()
  })

  it('renders field inspector panel with header', () => {
    render(<FieldInspectorPanel line={mockLine} onClose={() => {}} />)

    expect(screen.getByText('Field inspector')).toBeInTheDocument()
    expect(screen.getByTestId('field-inspector-panel')).toBeInTheDocument()
  })

  it('renders core fields in canonical order', () => {
    render(<FieldInspectorPanel line={mockLine} onClose={() => {}} />)

    const fieldRows = screen.getAllByTestId(/^field-row-/)
    const firstSixRows = fieldRows.slice(0, 6)

    expect(firstSixRows[0]).toHaveAttribute('data-testid', 'field-row-timestamp')
    expect(firstSixRows[1]).toHaveAttribute('data-testid', 'field-row-severity')
    expect(firstSixRows[2]).toHaveAttribute('data-testid', 'field-row-service')
    expect(firstSixRows[3]).toHaveAttribute('data-testid', 'field-row-host')
    expect(firstSixRows[4]).toHaveAttribute('data-testid', 'field-row-stream')
    expect(firstSixRows[5]).toHaveAttribute('data-testid', 'field-row-message')
  })

  it('renders bag entries alphabetically after core fields', () => {
    render(<FieldInspectorPanel line={mockLine} onClose={() => {}} />)

    const fieldRows = screen.getAllByTestId(/^field-row-/)
    const bagRows = fieldRows.slice(6)

    expect(bagRows[0]).toHaveAttribute('data-testid', 'field-row-alpha')
    expect(bagRows[1]).toHaveAttribute('data-testid', 'field-row-severity_raw')
    expect(bagRows[2]).toHaveAttribute('data-testid', 'field-row-source_type')
    expect(bagRows[3]).toHaveAttribute('data-testid', 'field-row-zeta')
  })

  it('calls onClose when close button clicked', async () => {
    const onCloseMock = vi.fn()
    render(<FieldInspectorPanel line={mockLine} onClose={onCloseMock} />)

    const closeButton = screen.getByTestId('field-inspector-close')
    await userEvent.click(closeButton)

    expect(onCloseMock).toHaveBeenCalledTimes(1)
  })

  it('renders Copy button for all fields', () => {
    render(<FieldInspectorPanel line={mockLine} onClose={() => {}} />)

    expect(screen.getByTestId('field-copy-timestamp')).toBeInTheDocument()
    expect(screen.getByTestId('field-copy-severity')).toBeInTheDocument()
    expect(screen.getByTestId('field-copy-service')).toBeInTheDocument()
    expect(screen.getByTestId('field-copy-host')).toBeInTheDocument()
    expect(screen.getByTestId('field-copy-stream')).toBeInTheDocument()
    expect(screen.getByTestId('field-copy-message')).toBeInTheDocument()
  })

  it('does not render add-filter button for timestamp', () => {
    const onAddMsgFilterMock = vi.fn()
    render(
      <FieldInspectorPanel
        line={mockLine}
        onClose={() => {}}
        onAddMsgFilter={onAddMsgFilterMock}
      />,
    )

    expect(screen.queryByTestId('field-add-filter-timestamp')).not.toBeInTheDocument()
  })

  it('renders add-to-filter buttons for non-timestamp fields when callbacks present', () => {
    const onAddServiceFilterMock = vi.fn()
    const onAddMsgFilterMock = vi.fn()
    const onAddFieldFilterMock = vi.fn()
    render(
      <FieldInspectorPanel
        line={mockLine}
        onClose={() => {}}
        onAddServiceFilter={onAddServiceFilterMock}
        onAddMsgFilter={onAddMsgFilterMock}
        onAddFieldFilter={onAddFieldFilterMock}
      />,
    )

    expect(screen.getByTestId('field-add-filter-severity')).toBeInTheDocument()
    expect(screen.getByTestId('field-add-filter-service')).toBeInTheDocument()
    expect(screen.getByTestId('field-add-filter-host')).toBeInTheDocument()
    expect(screen.getByTestId('field-add-filter-stream')).toBeInTheDocument()
    expect(screen.getByTestId('field-add-filter-message')).toBeInTheDocument()
    expect(screen.getByTestId('field-add-filter-alpha')).toBeInTheDocument()
    // severity_raw is a backend-only field not queryable in VictoriaLogs; add-filter button is suppressed
    // but the row and copy button still render
    expect(screen.queryByTestId('field-add-filter-severity_raw')).not.toBeInTheDocument()
    expect(screen.getByTestId('field-row-severity_raw')).toBeInTheDocument()
    expect(screen.getByTestId('field-copy-severity_raw')).toBeInTheDocument()
  })

  it('does not render add-filter buttons when callbacks absent', () => {
    render(<FieldInspectorPanel line={mockLine} onClose={() => {}} />)

    expect(screen.queryByTestId('field-add-filter-severity')).not.toBeInTheDocument()
    expect(screen.queryByTestId('field-add-filter-service')).not.toBeInTheDocument()
    expect(screen.queryByTestId('field-add-filter-host')).not.toBeInTheDocument()
  })

  it('hides host add-filter button when onAddFieldFilter absent (even if onAddMsgFilter present)', () => {
    const onAddMsgFilterMock = vi.fn()
    render(
      <FieldInspectorPanel
        line={mockLine}
        onClose={() => {}}
        onAddMsgFilter={onAddMsgFilterMock}
      />,
    )

    // host/severity route to onAddFieldFilter — absent → no button
    expect(screen.queryByTestId('field-add-filter-host')).not.toBeInTheDocument()
    expect(screen.queryByTestId('field-add-filter-severity')).not.toBeInTheDocument()
    // message/stream still route to onAddMsgFilter — present → button shown
    expect(screen.getByTestId('field-add-filter-message')).toBeInTheDocument()
    expect(screen.getByTestId('field-add-filter-stream')).toBeInTheDocument()
  })

  it('calls onAddServiceFilter with service and source_type when service add button clicked', async () => {
    const onAddServiceFilterMock = vi.fn()
    const onAddMsgFilterMock = vi.fn()
    render(
      <FieldInspectorPanel
        line={mockLine}
        onClose={() => {}}
        onAddServiceFilter={onAddServiceFilterMock}
        onAddMsgFilter={onAddMsgFilterMock}
      />,
    )

    const serviceAddButton = screen.getByTestId('field-add-filter-service')
    await userEvent.click(serviceAddButton)

    expect(onAddServiceFilterMock).toHaveBeenCalledWith('nginx', 'docker')
    expect(onAddMsgFilterMock).not.toHaveBeenCalled()
  })

  it('calls onAddFieldFilter for host row when add button clicked', async () => {
    const onAddServiceFilterMock = vi.fn()
    const onAddMsgFilterMock = vi.fn()
    const onAddFieldFilterMock = vi.fn()
    render(
      <FieldInspectorPanel
        line={mockLine}
        onClose={() => {}}
        onAddServiceFilter={onAddServiceFilterMock}
        onAddMsgFilter={onAddMsgFilterMock}
        onAddFieldFilter={onAddFieldFilterMock}
      />,
    )

    const hostAddButton = screen.getByTestId('field-add-filter-host')
    await userEvent.click(hostAddButton)

    expect(onAddFieldFilterMock).toHaveBeenCalledWith('host', 'host-1')
    expect(onAddMsgFilterMock).not.toHaveBeenCalled()
    expect(onAddServiceFilterMock).not.toHaveBeenCalled()
  })

  it('calls onAddMsgFilter for message row when add button clicked', async () => {
    const onAddServiceFilterMock = vi.fn()
    const onAddMsgFilterMock = vi.fn()
    const onAddFieldFilterMock = vi.fn()
    render(
      <FieldInspectorPanel
        line={mockLine}
        onClose={() => {}}
        onAddServiceFilter={onAddServiceFilterMock}
        onAddMsgFilter={onAddMsgFilterMock}
        onAddFieldFilter={onAddFieldFilterMock}
      />,
    )

    const msgAddButton = screen.getByTestId('field-add-filter-message')
    await userEvent.click(msgAddButton)

    expect(onAddMsgFilterMock).toHaveBeenCalledWith('Request received')
    expect(onAddFieldFilterMock).not.toHaveBeenCalled()
    expect(onAddServiceFilterMock).not.toHaveBeenCalled()
  })

  it('calls onAddMsgFilter for stream row when add button clicked (stream maps to VL _stream_id, not queryable flat field)', async () => {
    const onAddMsgFilterMock = vi.fn()
    const onAddFieldFilterMock = vi.fn()
    render(
      <FieldInspectorPanel
        line={mockLine}
        onClose={() => {}}
        onAddMsgFilter={onAddMsgFilterMock}
        onAddFieldFilter={onAddFieldFilterMock}
      />,
    )

    const streamAddButton = screen.getByTestId('field-add-filter-stream')
    await userEvent.click(streamAddButton)

    expect(onAddMsgFilterMock).toHaveBeenCalledWith('stdout')
    expect(onAddFieldFilterMock).not.toHaveBeenCalled()
  })

  it('calls onAddFieldFilter for severity row with raw stored value (not normalized display)', async () => {
    const onAddMsgFilterMock = vi.fn()
    const onAddFieldFilterMock = vi.fn()
    render(
      <FieldInspectorPanel
        line={mockLine}
        onClose={() => {}}
        onAddMsgFilter={onAddMsgFilterMock}
        onAddFieldFilter={onAddFieldFilterMock}
      />,
    )

    const sevAddButton = screen.getByTestId('field-add-filter-severity')
    await userEvent.click(sevAddButton)

    // mockLine.severity is 'info' (normalized display) but fields['severity_raw']
    // is 'INFO' — the filter must use the raw value because VL indexed 'INFO'.
    expect(onAddFieldFilterMock).toHaveBeenCalledWith('severity', 'INFO')
    expect(onAddMsgFilterMock).not.toHaveBeenCalled()
  })

  it('calls onAddFieldFilter for severity row with numeric raw value when severity_raw is a syslog numeric', async () => {
    // Simulate a journald line: VL stored severity "4" (numeric PRIORITY),
    // normalized display is "warn". The filter must use "4" — that is what VL indexed.
    const journaldLine: LogLine = {
      ...mockLine,
      severity: 'warn',
      fields: {
        ...mockLine.fields,
        severity_raw: '4',
      },
    }
    const onAddFieldFilterMock = vi.fn()
    render(
      <FieldInspectorPanel
        line={journaldLine}
        onClose={() => {}}
        onAddFieldFilter={onAddFieldFilterMock}
      />,
    )

    const sevAddButton = screen.getByTestId('field-add-filter-severity')
    await userEvent.click(sevAddButton)

    expect(onAddFieldFilterMock).toHaveBeenCalledWith('severity', '4')
  })

  it('calls onAddFieldFilter for severity row with display value when severity_raw is absent (fallback)', async () => {
    // Defensive: if a line somehow arrives without severity_raw in fields,
    // the filter falls back to the normalized display value.
    const lineNoRaw: LogLine = {
      ...mockLine,
      severity: 'error',
      fields: {
        source_type: 'docker',
        // no severity_raw
      },
    }
    const onAddFieldFilterMock = vi.fn()
    render(
      <FieldInspectorPanel
        line={lineNoRaw}
        onClose={() => {}}
        onAddFieldFilter={onAddFieldFilterMock}
      />,
    )

    const sevAddButton = screen.getByTestId('field-add-filter-severity')
    await userEvent.click(sevAddButton)

    expect(onAddFieldFilterMock).toHaveBeenCalledWith('severity', 'error')
  })

  it('calls onAddFieldFilter for bag entry rows when add button clicked', async () => {
    const onAddMsgFilterMock = vi.fn()
    const onAddFieldFilterMock = vi.fn()
    render(
      <FieldInspectorPanel
        line={mockLine}
        onClose={() => {}}
        onAddMsgFilter={onAddMsgFilterMock}
        onAddFieldFilter={onAddFieldFilterMock}
      />,
    )

    const alphaAddButton = screen.getByTestId('field-add-filter-alpha')
    await userEvent.click(alphaAddButton)

    expect(onAddFieldFilterMock).toHaveBeenCalledWith('alpha', 'a')
    expect(onAddMsgFilterMock).not.toHaveBeenCalled()
  })

  it('omits rows for null core field values', () => {
    const lineWithNull: LogLine = {
      ...mockLine,
      host: null,
      service: null,
    }

    render(<FieldInspectorPanel line={lineWithNull} onClose={() => {}} />)

    // Null core fields are not rendered at all.
    expect(screen.queryByTestId('field-row-host')).not.toBeInTheDocument()
    expect(screen.queryByTestId('field-row-service')).not.toBeInTheDocument()
    // Present fields still render.
    expect(screen.getByTestId('field-row-severity')).toBeInTheDocument()
    expect(screen.getByTestId('field-row-message')).toBeInTheDocument()
  })

  it('omits the Copy button for null values (row not rendered)', () => {
    const lineWithNull: LogLine = {
      ...mockLine,
      host: null,
    }

    render(<FieldInspectorPanel line={lineWithNull} onClose={() => {}} />)

    expect(screen.queryByTestId('field-copy-host')).not.toBeInTheDocument()
  })

  it('does not render add-filter button for null values', () => {
    const lineWithNull: LogLine = {
      ...mockLine,
      host: null,
    }

    const onAddMsgFilterMock = vi.fn()
    render(
      <FieldInspectorPanel
        line={lineWithNull}
        onClose={() => {}}
        onAddMsgFilter={onAddMsgFilterMock}
      />,
    )

    expect(screen.queryByTestId('field-add-filter-host')).not.toBeInTheDocument()
  })

  it('uses "unknown" as default sourceType when source_type field missing', async () => {
    const lineWithoutSourceType: LogLine = {
      ...mockLine,
      fields: {
        alpha: 'a',
      },
    }

    const onAddServiceFilterMock = vi.fn()
    render(
      <FieldInspectorPanel
        line={lineWithoutSourceType}
        onClose={() => {}}
        onAddServiceFilter={onAddServiceFilterMock}
      />,
    )

    const serviceAddButton = screen.getByTestId('field-add-filter-service')
    await userEvent.click(serviceAddButton)

    expect(onAddServiceFilterMock).toHaveBeenCalledWith('nginx', 'unknown')
  })

  it('omits bag rows with empty string values', () => {
    const lineWithEmptyBag: LogLine = {
      ...mockLine,
      fields: {
        empty_field: '',
        normal_field: 'value',
      },
    }

    render(<FieldInspectorPanel line={lineWithEmptyBag} onClose={() => {}} />)

    // Empty string bag field must be omitted entirely.
    expect(screen.queryByTestId('field-row-empty_field')).not.toBeInTheDocument()
    // Non-empty bag field still renders.
    expect(screen.getByTestId('field-row-normal_field')).toBeInTheDocument()
  })

  it('omits bag rows with whitespace-only string values', () => {
    const lineWithWhitespaceBag: LogLine = {
      ...mockLine,
      fields: {
        whitespace_field: '   ',
        normal_field: 'value',
      },
    }

    render(<FieldInspectorPanel line={lineWithWhitespaceBag} onClose={() => {}} />)

    expect(screen.queryByTestId('field-row-whitespace_field')).not.toBeInTheDocument()
    expect(screen.getByTestId('field-row-normal_field')).toBeInTheDocument()
  })

  it('omits the entire row (no copy or add-filter button) for empty string bag values', () => {
    const lineWithEmptyBag: LogLine = {
      ...mockLine,
      fields: {
        empty_field: '',
      },
    }

    const onAddMsgFilterMock = vi.fn()
    render(
      <FieldInspectorPanel
        line={lineWithEmptyBag}
        onClose={() => {}}
        onAddMsgFilter={onAddMsgFilterMock}
      />,
    )

    expect(screen.queryByTestId('field-row-empty_field')).not.toBeInTheDocument()
    expect(screen.queryByTestId('field-copy-empty_field')).not.toBeInTheDocument()
    expect(screen.queryByTestId('field-add-filter-empty_field')).not.toBeInTheDocument()
  })

  it('renders non-null falsy values (0, false) as visible rows', () => {
    const lineWithFalsy: LogLine = {
      ...mockLine,
      fields: {
        zero_field: 0,
        false_field: false,
      },
    }

    render(<FieldInspectorPanel line={lineWithFalsy} onClose={() => {}} />)

    expect(screen.getByTestId('field-row-zero_field')).toBeInTheDocument()
    expect(screen.getByTestId('field-row-false_field')).toBeInTheDocument()
  })

  it('renders non-string bag values via String(value)', () => {
    const lineWithMixedBag: LogLine = {
      ...mockLine,
      fields: {
        number_field: 42,
        bool_field: true,
        object_field: { key: 'value' },
      },
    }

    render(<FieldInspectorPanel line={lineWithMixedBag} onClose={() => {}} />)

    expect(screen.getByTestId('field-row-bool_field')).toHaveTextContent('true')
    expect(screen.getByTestId('field-row-number_field')).toHaveTextContent('42')
    expect(screen.getByTestId('field-row-object_field')).toHaveTextContent('[object Object]')
  })
})
