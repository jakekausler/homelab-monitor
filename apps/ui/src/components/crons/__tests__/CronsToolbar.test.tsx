import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { CronsToolbar, type ToolbarFilters } from '@/components/crons/CronsToolbar'

afterEach(cleanup)

const defaultFilters: ToolbarFilters = { include_hidden: false }

describe('CronsToolbar', () => {
  it('renders search input, selects, and filter controls', () => {
    render(<CronsToolbar filters={defaultFilters} knownHosts={[]} onFiltersChange={vi.fn()} />)
    expect(screen.getByRole('textbox', { name: /Search by name/i })).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: /Filter by host/i })).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: /Filter by state/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Add cron/i })).toBeNull()
  })

  it('renders known hosts as select options', () => {
    render(
      <CronsToolbar
        filters={defaultFilters}
        knownHosts={['host-a', 'host-b']}
        onFiltersChange={vi.fn()}
      />,
    )
    expect(screen.getByRole('option', { name: 'host-a' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'host-b' })).toBeInTheDocument()
  })

  it('calls onFiltersChange when host select changes', async () => {
    const onFiltersChange = vi.fn()
    render(
      <CronsToolbar
        filters={defaultFilters}
        knownHosts={['host-a']}
        onFiltersChange={onFiltersChange}
      />,
    )
    await userEvent
      .setup()
      .selectOptions(screen.getByRole('combobox', { name: /Filter by host/i }), 'host-a')
    expect(onFiltersChange).toHaveBeenCalledWith(expect.objectContaining({ host: 'host-a' }))
  })

  it('calls onFiltersChange with include_hidden when checkbox toggled', async () => {
    const onFiltersChange = vi.fn()
    render(
      <CronsToolbar filters={defaultFilters} knownHosts={[]} onFiltersChange={onFiltersChange} />,
    )
    await userEvent.setup().click(screen.getByRole('checkbox'))
    expect(onFiltersChange).toHaveBeenCalledWith(expect.objectContaining({ include_hidden: true }))
  })

  it('calls onFiltersChange when state select changes', async () => {
    const onFiltersChange = vi.fn()
    render(
      <CronsToolbar filters={defaultFilters} knownHosts={[]} onFiltersChange={onFiltersChange} />,
    )
    await userEvent
      .setup()
      .selectOptions(screen.getByRole('combobox', { name: /Filter by state/i }), 'failed')
    expect(onFiltersChange).toHaveBeenCalledWith(expect.objectContaining({ state: 'failed' }))
  })

  it('clears host filter when All hosts is selected', async () => {
    const onFiltersChange = vi.fn()
    render(
      <CronsToolbar
        filters={{ ...defaultFilters, host: 'host-a' }}
        knownHosts={['host-a']}
        onFiltersChange={onFiltersChange}
      />,
    )
    await userEvent
      .setup()
      .selectOptions(screen.getByRole('combobox', { name: /Filter by host/i }), 'All hosts')
    const call = onFiltersChange.mock.calls[0]?.[0] as ToolbarFilters
    expect(call.host).toBeUndefined()
  })
})
