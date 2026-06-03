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
    severity: 'INFO',
    service: 'nginx',
    host: 'host-1',
    stream: 'stdout',
    message: 'Request received',
    fields: {
      source_type: 'docker',
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
    expect(bagRows[1]).toHaveAttribute('data-testid', 'field-row-source_type')
    expect(bagRows[2]).toHaveAttribute('data-testid', 'field-row-zeta')
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
    render(
      <FieldInspectorPanel
        line={mockLine}
        onClose={() => {}}
        onAddServiceFilter={onAddServiceFilterMock}
        onAddMsgFilter={onAddMsgFilterMock}
      />,
    )

    expect(screen.getByTestId('field-add-filter-severity')).toBeInTheDocument()
    expect(screen.getByTestId('field-add-filter-service')).toBeInTheDocument()
    expect(screen.getByTestId('field-add-filter-host')).toBeInTheDocument()
    expect(screen.getByTestId('field-add-filter-stream')).toBeInTheDocument()
    expect(screen.getByTestId('field-add-filter-message')).toBeInTheDocument()
    expect(screen.getByTestId('field-add-filter-alpha')).toBeInTheDocument()
  })

  it('does not render add-filter buttons when callbacks absent', () => {
    render(<FieldInspectorPanel line={mockLine} onClose={() => {}} />)

    expect(screen.queryByTestId('field-add-filter-severity')).not.toBeInTheDocument()
    expect(screen.queryByTestId('field-add-filter-service')).not.toBeInTheDocument()
    expect(screen.queryByTestId('field-add-filter-host')).not.toBeInTheDocument()
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

  it('calls onAddMsgFilter for non-service fields when add button clicked', async () => {
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

    const hostAddButton = screen.getByTestId('field-add-filter-host')
    await userEvent.click(hostAddButton)

    expect(onAddMsgFilterMock).toHaveBeenCalledWith('host-1')
    expect(onAddServiceFilterMock).not.toHaveBeenCalled()
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
