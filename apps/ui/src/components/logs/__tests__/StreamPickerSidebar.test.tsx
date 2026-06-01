import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { StreamPickerSidebar } from '@/components/logs/StreamPickerSidebar'
import type { Schema } from '@/api/types'

afterEach(cleanup)

type ServiceCount = Schema<'ServiceCount'>

describe('StreamPickerSidebar', () => {
  const mockServices: ServiceCount[] = [
    { service: 'nginx', count: 1000 },
    { service: 'home-assistant', count: 500 },
  ]

  it('renders header and service rows', () => {
    const onToggle = vi.fn()
    render(
      <StreamPickerSidebar
        services={mockServices}
        truncated={false}
        selectedServices={[]}
        onToggleService={onToggle}
        isLoading={false}
        isError={false}
      />,
    )
    expect(screen.getByText('Services')).toBeInTheDocument()
    const rows = screen.getAllByTestId('stream-picker-row')
    expect(rows).toHaveLength(2)
  })

  it('formats counts with toLocaleString', () => {
    const onToggle = vi.fn()
    render(
      <StreamPickerSidebar
        services={[{ service: 'nginx', count: 1000 }]}
        truncated={false}
        selectedServices={[]}
        onToggleService={onToggle}
        isLoading={false}
        isError={false}
      />,
    )
    expect(screen.getByText('1,000')).toBeInTheDocument()
  })

  it('shows selected state with aria-pressed and styling', () => {
    const onToggle = vi.fn()
    render(
      <StreamPickerSidebar
        services={mockServices}
        truncated={false}
        selectedServices={['nginx']}
        onToggleService={onToggle}
        isLoading={false}
        isError={false}
      />,
    )
    const rows = screen.getAllByTestId('stream-picker-row')
    const nginxRow = rows[0]
    expect(nginxRow).toHaveAttribute('aria-pressed', 'true')
    expect(nginxRow).toHaveClass('bg-accent')
  })

  it('calls onToggleService when a row is clicked', () => {
    const onToggle = vi.fn()
    render(
      <StreamPickerSidebar
        services={mockServices}
        truncated={false}
        selectedServices={[]}
        onToggleService={onToggle}
        isLoading={false}
        isError={false}
      />,
    )
    const rows = screen.getAllByTestId('stream-picker-row')
    fireEvent.click(rows[0]!)
    expect(onToggle).toHaveBeenCalledWith('nginx')
  })

  it('shows truncated banner when truncated is true', () => {
    const onShowMore = vi.fn()
    render(
      <StreamPickerSidebar
        services={mockServices}
        truncated={true}
        selectedServices={[]}
        onToggleService={vi.fn()}
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
        selectedServices={[]}
        onToggleService={vi.fn()}
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
        selectedServices={[]}
        onToggleService={vi.fn()}
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
        selectedServices={[]}
        onToggleService={vi.fn()}
        isLoading={false}
        isError={false}
      />,
    )
    expect(screen.getByTestId('stream-picker-empty')).toBeInTheDocument()
  })
})
