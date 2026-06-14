import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import type { ApiError } from '@/api/client'

import { DrillList } from './DrillList'

afterEach(() => {
  cleanup()
})

interface Item {
  name: string
}

function renderRow(item: Item) {
  return <span>{item.name}</span>
}

function make502(): ApiError {
  const err = new Error('bad gateway') as ApiError & { status: number }
  err.status = 502
  return err
}

function make500(): ApiError {
  const err = new Error('Server error') as ApiError & { status: number }
  err.status = 500
  return err
}

describe('DrillList', () => {
  it('renders rows via renderRow', () => {
    render(
      <DrillList<Item>
        items={[{ name: 'alpha' }, { name: 'beta' }]}
        isPending={false}
        error={null}
        renderRow={renderRow}
        emptyLabel="nothing here"
        total={2}
        returned={2}
      />,
    )
    expect(screen.getByText('alpha')).toBeInTheDocument()
    expect(screen.getByText('beta')).toBeInTheDocument()
  })

  it('shows the empty label when there are no items', () => {
    render(
      <DrillList<Item>
        items={[]}
        isPending={false}
        error={null}
        renderRow={renderRow}
        emptyLabel="nothing here"
        total={0}
        returned={0}
      />,
    )
    expect(screen.getByText('nothing here')).toBeInTheDocument()
  })

  it('shows Loading… when pending', () => {
    render(
      <DrillList<Item>
        items={[]}
        isPending={true}
        error={null}
        renderRow={renderRow}
        emptyLabel="nothing here"
        total={0}
        returned={0}
      />,
    )
    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('shows the 502 banner when error status is 502', () => {
    render(
      <DrillList<Item>
        items={[]}
        isPending={false}
        error={make502()}
        renderRow={renderRow}
        emptyLabel="nothing here"
        total={0}
        returned={0}
      />,
    )
    expect(screen.getByText('Home Assistant metrics temporarily unavailable')).toBeInTheDocument()
  })

  it('shows ErrorDisplay for non-502 errors', () => {
    render(
      <DrillList<Item>
        items={[]}
        isPending={false}
        error={make500()}
        renderRow={renderRow}
        emptyLabel="nothing here"
        total={0}
        returned={0}
      />,
    )
    expect(screen.getByRole('alert')).toBeInTheDocument()
    expect(screen.getByText('Server error')).toBeInTheDocument()
  })

  it('shows the cap caption with ordering suffix when total > returned', () => {
    render(
      <DrillList<Item>
        items={[{ name: 'alpha' }]}
        isPending={false}
        error={null}
        renderRow={renderRow}
        emptyLabel="nothing here"
        total={100}
        returned={1}
        orderingLabel="stalest first"
      />,
    )
    expect(screen.getByText('Showing 1 of 100 — stalest first')).toBeInTheDocument()
  })

  it('shows the cap caption without suffix when no orderingLabel', () => {
    render(
      <DrillList<Item>
        items={[{ name: 'alpha' }]}
        isPending={false}
        error={null}
        renderRow={renderRow}
        emptyLabel="nothing here"
        total={5}
        returned={1}
      />,
    )
    expect(screen.getByText('Showing 1 of 5')).toBeInTheDocument()
  })

  it('does not show a cap caption when total equals returned', () => {
    render(
      <DrillList<Item>
        items={[{ name: 'alpha' }]}
        isPending={false}
        error={null}
        renderRow={renderRow}
        emptyLabel="nothing here"
        total={1}
        returned={1}
      />,
    )
    expect(screen.queryByText(/^Showing/)).not.toBeInTheDocument()
  })

  it('applies the scroll container class when row count exceeds the threshold', () => {
    const items = Array.from({ length: 9 }, (_, i) => ({ name: `row-${String(i)}` }))
    const { container } = render(
      <DrillList<Item>
        items={items}
        isPending={false}
        error={null}
        renderRow={renderRow}
        emptyLabel="nothing here"
        total={9}
        returned={9}
      />,
    )
    const list = container.querySelector('ul')
    expect(list?.className).toContain('max-h-80')
    expect(list?.className).toContain('overflow-y-auto')
  })

  it('does not apply the scroll container class at or below the threshold', () => {
    const items = Array.from({ length: 8 }, (_, i) => ({ name: `row-${String(i)}` }))
    const { container } = render(
      <DrillList<Item>
        items={items}
        isPending={false}
        error={null}
        renderRow={renderRow}
        emptyLabel="nothing here"
        total={8}
        returned={8}
      />,
    )
    const list = container.querySelector('ul')
    expect(list?.className).not.toContain('max-h-80')
  })
})
