import React from 'react'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { TooltipProvider } from '@/components/ui/tooltip'
import { ColumnsControl } from '../ColumnsControl'

const renderWithProvider = (ui: React.ReactElement) =>
  render(<TooltipProvider>{ui}</TooltipProvider>)

afterEach(cleanup)

async function openMenu(): Promise<void> {
  await userEvent.click(screen.getByTestId('logs-columns-toggle'))
}

describe('ColumnsControl', () => {
  it('always shows the Common columns', async () => {
    renderWithProvider(<ColumnsControl selected={[]} available={[]} onChange={vi.fn()} />)
    await openMenu()
    const fields = screen
      .getAllByTestId('logs-column-option')
      .map((o) => o.getAttribute('data-field'))
    expect(fields).toEqual(expect.arrayContaining(['service', 'host', 'severity']))
  })

  it('lists discovered fields (deduped against Common)', async () => {
    renderWithProvider(
      <ColumnsControl selected={[]} available={['region', 'service', 'pod']} onChange={vi.fn()} />,
    )
    await openMenu()
    const fields = screen
      .getAllByTestId('logs-column-option')
      .map((o) => o.getAttribute('data-field'))
    expect(fields).toContain('region')
    expect(fields).toContain('pod')
    // 'service' from available must NOT be duplicated — appears once (the Common one).
    expect(fields.filter((f) => f === 'service')).toHaveLength(1)
  })

  it('toggling an unselected field calls onChange with it appended', async () => {
    const onChange = vi.fn()
    renderWithProvider(
      <ColumnsControl selected={['host']} available={['region']} onChange={onChange} />,
    )
    await openMenu()
    const regionOption = screen
      .getAllByTestId('logs-column-option')
      .find((o) => o.getAttribute('data-field') === 'region')
    await userEvent.click(regionOption!)
    expect(onChange).toHaveBeenCalledWith(['host', 'region'])
  })

  it('toggling a selected field calls onChange with it removed', async () => {
    const onChange = vi.fn()
    renderWithProvider(
      <ColumnsControl selected={['host', 'region']} available={['region']} onChange={onChange} />,
    )
    await openMenu()
    const hostOption = screen
      .getAllByTestId('logs-column-option')
      .find((o) => o.getAttribute('data-field') === 'host')
    await userEvent.click(hostOption!)
    expect(onChange).toHaveBeenCalledWith(['region'])
  })

  it('shows a selected-but-undiscovered field as a checked, removable option', async () => {
    const onChange = vi.fn()
    renderWithProvider(
      <ColumnsControl selected={['ghostfield']} available={[]} onChange={onChange} />,
    )
    await openMenu()
    const ghost = screen
      .getAllByTestId('logs-column-option')
      .find((o) => o.getAttribute('data-field') === 'ghostfield')
    expect(ghost).toBeDefined()
    // radix marks the checked item with aria-checked / data-state="checked"
    expect(ghost?.getAttribute('data-state')).toBe('checked')
    await userEvent.click(ghost!)
    expect(onChange).toHaveBeenCalledWith([])
  })
})
