import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { StreamPickerSidebar } from '@/components/logs/StreamPickerSidebar'
import type { Schema } from '@/api/types'

afterEach(cleanup)

type ServiceCount = Schema<'ServiceCount'>

describe('StreamPickerSidebar', () => {
  const mockServices: ServiceCount[] = [
    { service: 'nginx', source_type: 'docker', count: 1000 },
    { service: 'home-assistant', source_type: 'docker', count: 500 },
    { service: 'backup', source_type: 'cron', count: 300 },
    { service: 'sshd', source_type: 'systemd', count: 50 },
    { service: 'nginx', source_type: 'systemd', count: 7 }, // same name, different type
    { service: 'mystery', source_type: 'unknown', count: 2 },
  ]

  it('groups into per-source_type sections', () => {
    render(
      <StreamPickerSidebar
        services={mockServices}
        truncated={false}
        selectedIdentities={[]}
        onToggleIdentity={vi.fn()}
        onSelectIdentities={vi.fn()}
        onDeselectIdentities={vi.fn()}
        isLoading={false}
        isError={false}
      />,
    )
    const sections = screen.getAllByTestId('stream-picker-section')
    expect(sections).toHaveLength(4) // docker, cron, systemd, unknown
  })

  it('section order is docker, cron, systemd, unknown-last', () => {
    render(
      <StreamPickerSidebar
        services={mockServices}
        truncated={false}
        selectedIdentities={[]}
        onToggleIdentity={vi.fn()}
        onSelectIdentities={vi.fn()}
        onDeselectIdentities={vi.fn()}
        isLoading={false}
        isError={false}
      />,
    )
    const sections = screen.getAllByTestId('stream-picker-section')
    const sourceTypes = sections.map((s) => s.getAttribute('data-source-type'))
    expect(sourceTypes).toEqual(['docker', 'cron', 'systemd', 'unknown'])
  })

  it('same service name appears in two sections', () => {
    render(
      <StreamPickerSidebar
        services={mockServices}
        truncated={false}
        selectedIdentities={[]}
        onToggleIdentity={vi.fn()}
        onSelectIdentities={vi.fn()}
        onDeselectIdentities={vi.fn()}
        isLoading={false}
        isError={false}
      />,
    )
    const rows = screen.getAllByTestId('stream-picker-row')
    const nginxRows = rows.filter((r) => r.getAttribute('data-service') === 'nginx')
    expect(nginxRows).toHaveLength(2)
    const sourceTypes = nginxRows.map((r) => r.getAttribute('data-source-type'))
    expect(sourceTypes).toContain('docker')
    expect(sourceTypes).toContain('systemd')
  })

  it('rows within a section are sorted by count desc', () => {
    render(
      <StreamPickerSidebar
        services={mockServices}
        truncated={false}
        selectedIdentities={[]}
        onToggleIdentity={vi.fn()}
        onSelectIdentities={vi.fn()}
        onDeselectIdentities={vi.fn()}
        isLoading={false}
        isError={false}
      />,
    )
    // Docker section should have nginx (1000) before home-assistant (500)
    const rows = screen.getAllByTestId('stream-picker-row')
    const dockerRows = rows.filter((r) => r.getAttribute('data-source-type') === 'docker')
    expect(dockerRows[0]?.getAttribute('data-service')).toBe('nginx')
    expect(dockerRows[1]?.getAttribute('data-service')).toBe('home-assistant')
  })

  it('clicking a row toggles that identity', () => {
    const onToggle = vi.fn()
    render(
      <StreamPickerSidebar
        services={mockServices}
        truncated={false}
        selectedIdentities={[]}
        onToggleIdentity={onToggle}
        onSelectIdentities={vi.fn()}
        onDeselectIdentities={vi.fn()}
        isLoading={false}
        isError={false}
      />,
    )
    const rows = screen.getAllByTestId('stream-picker-row')
    const nginxDockerRow = rows.find(
      (r) =>
        r.getAttribute('data-service') === 'nginx' &&
        r.getAttribute('data-source-type') === 'docker',
    )
    fireEvent.click(nginxDockerRow!)
    expect(onToggle).toHaveBeenCalledWith({
      service: 'nginx',
      source_type: 'docker',
    })
  })

  it('collapse toggle hides section rows', () => {
    render(
      <StreamPickerSidebar
        services={mockServices}
        truncated={false}
        selectedIdentities={[]}
        onToggleIdentity={vi.fn()}
        onSelectIdentities={vi.fn()}
        onDeselectIdentities={vi.fn()}
        isLoading={false}
        isError={false}
      />,
    )
    const toggles = screen.getAllByTestId('stream-picker-section-toggle')
    const dockerToggle = toggles[0]! // First section is docker
    // Initially open
    let dockerRows = screen
      .getAllByTestId('stream-picker-row')
      .filter((r) => r.getAttribute('data-source-type') === 'docker')
    expect(dockerRows.length).toBeGreaterThan(0)
    // Click to collapse
    fireEvent.click(dockerToggle)
    dockerRows = screen
      .queryAllByTestId('stream-picker-row')
      .filter((r) => r.getAttribute('data-source-type') === 'docker')
    expect(dockerRows).toHaveLength(0)
    // Click to expand again
    fireEvent.click(dockerToggle)
    dockerRows = screen
      .getAllByTestId('stream-picker-row')
      .filter((r) => r.getAttribute('data-source-type') === 'docker')
    expect(dockerRows.length).toBeGreaterThan(0)
  })

  it('select-all unions listed identities', () => {
    const onSelectIdentities = vi.fn()
    render(
      <StreamPickerSidebar
        services={mockServices}
        truncated={false}
        selectedIdentities={[]}
        onToggleIdentity={vi.fn()}
        onSelectIdentities={onSelectIdentities}
        onDeselectIdentities={vi.fn()}
        isLoading={false}
        isError={false}
      />,
    )
    const selectAlls = screen.getAllByTestId('stream-picker-section-selectall')
    const dockerSelectAll = selectAlls[0]! // First section is docker
    fireEvent.click(dockerSelectAll)
    expect(onSelectIdentities).toHaveBeenCalledWith(
      expect.arrayContaining([
        { service: 'nginx', source_type: 'docker' },
        { service: 'home-assistant', source_type: 'docker' },
      ]),
    )
  })

  it('select-none removes listed identities', () => {
    const onDeselectIdentities = vi.fn()
    render(
      <StreamPickerSidebar
        services={mockServices}
        truncated={false}
        selectedIdentities={[
          { service: 'nginx', source_type: 'docker' },
          { service: 'home-assistant', source_type: 'docker' },
        ]}
        onToggleIdentity={vi.fn()}
        onSelectIdentities={vi.fn()}
        onDeselectIdentities={onDeselectIdentities}
        isLoading={false}
        isError={false}
      />,
    )
    const selectAlls = screen.getAllByTestId('stream-picker-section-selectall')
    const dockerSelectAll = selectAlls[0]! // First section is docker
    fireEvent.click(dockerSelectAll)
    expect(onDeselectIdentities).toHaveBeenCalledWith(
      expect.arrayContaining([
        { service: 'nginx', source_type: 'docker' },
        { service: 'home-assistant', source_type: 'docker' },
      ]),
    )
  })

  it('indeterminate when some selected', () => {
    render(
      <StreamPickerSidebar
        services={mockServices}
        truncated={false}
        selectedIdentities={[{ service: 'nginx', source_type: 'docker' }]}
        onToggleIdentity={vi.fn()}
        onSelectIdentities={vi.fn()}
        onDeselectIdentities={vi.fn()}
        isLoading={false}
        isError={false}
      />,
    )
    const selectAlls = screen.getAllByTestId('stream-picker-section-selectall')
    const dockerSelectAll = selectAlls[0]! // First section is docker
    expect(dockerSelectAll).toHaveAttribute('aria-checked', 'mixed')
  })

  it('selected row shows aria-pressed and bg-accent', () => {
    render(
      <StreamPickerSidebar
        services={mockServices}
        truncated={false}
        selectedIdentities={[{ service: 'nginx', source_type: 'docker' }]}
        onToggleIdentity={vi.fn()}
        onSelectIdentities={vi.fn()}
        onDeselectIdentities={vi.fn()}
        isLoading={false}
        isError={false}
      />,
    )
    const rows = screen.getAllByTestId('stream-picker-row')
    const nginxDockerRow = rows.find(
      (r) =>
        r.getAttribute('data-service') === 'nginx' &&
        r.getAttribute('data-source-type') === 'docker',
    )
    expect(nginxDockerRow).toHaveAttribute('aria-pressed', 'true')
    expect(nginxDockerRow).toHaveClass('bg-accent')
  })

  it('truncated banner renders', () => {
    const onShowMore = vi.fn()
    render(
      <StreamPickerSidebar
        services={mockServices}
        truncated={true}
        selectedIdentities={[]}
        onToggleIdentity={vi.fn()}
        onSelectIdentities={vi.fn()}
        onDeselectIdentities={vi.fn()}
        isLoading={false}
        isError={false}
        onShowMore={onShowMore}
      />,
    )
    expect(screen.getByTestId('stream-picker-truncated')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('stream-picker-truncated'))
    expect(onShowMore).toHaveBeenCalled()
  })

  it('shows loading state', () => {
    render(
      <StreamPickerSidebar
        services={[]}
        truncated={false}
        selectedIdentities={[]}
        onToggleIdentity={vi.fn()}
        onSelectIdentities={vi.fn()}
        onDeselectIdentities={vi.fn()}
        isLoading={true}
        isError={false}
      />,
    )
    expect(screen.getByTestId('stream-picker-loading')).toBeInTheDocument()
    expect(screen.queryAllByTestId('stream-picker-row')).toHaveLength(0)
  })

  it('shows error state', () => {
    render(
      <StreamPickerSidebar
        services={[]}
        truncated={false}
        selectedIdentities={[]}
        onToggleIdentity={vi.fn()}
        onSelectIdentities={vi.fn()}
        onDeselectIdentities={vi.fn()}
        isLoading={false}
        isError={true}
      />,
    )
    expect(screen.getByTestId('stream-picker-error')).toBeInTheDocument()
  })

  it('shows empty state when no services and not loading/error', () => {
    render(
      <StreamPickerSidebar
        services={[]}
        truncated={false}
        selectedIdentities={[]}
        onToggleIdentity={vi.fn()}
        onSelectIdentities={vi.fn()}
        onDeselectIdentities={vi.fn()}
        isLoading={false}
        isError={false}
      />,
    )
    expect(screen.getByTestId('stream-picker-empty')).toBeInTheDocument()
  })
})
