import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

const copyFn = vi.fn()
vi.mock('@/lib/useCopyToClipboard', () => ({
  useCopyToClipboard: () => copyFn,
}))

import { JsonMessageTree } from '../JsonMessageTree'

describe('JsonMessageTree', () => {
  beforeEach(() => {
    copyFn.mockClear()
  })

  afterEach(() => {
    cleanup()
  })

  it('renders object keys', () => {
    render(<JsonMessageTree value={{ collector: 'x', event: 'y' }} />)

    expect(screen.getByTestId('json-message-tree')).toBeInTheDocument()
    expect(screen.getByTestId('json-node-root')).toBeInTheDocument()
    expect(screen.getByTestId('json-node-toggle-root')).toBeInTheDocument()
    expect(screen.getByTestId('json-node-root.collector')).toBeInTheDocument()
    expect(screen.getByTestId('json-node-root.event')).toBeInTheDocument()
  })

  it('renders array indices', () => {
    render(<JsonMessageTree value={['a', 'b']} />)

    expect(screen.getByTestId('json-node-root[0]')).toBeInTheDocument()
    expect(screen.getByTestId('json-node-root[1]')).toBeInTheDocument()
  })

  it('expands top-level by default', () => {
    render(<JsonMessageTree value={{ collector: 'x', event: 'y' }} />)

    // Root's children should be visible without clicking
    expect(screen.getByTestId('json-node-root.collector')).toBeInTheDocument()
    expect(screen.getByTestId('json-node-root.event')).toBeInTheDocument()
  })

  it('collapses nested branches by default at depth > 0', async () => {
    const user = userEvent.setup()
    render(<JsonMessageTree value={{ outer: { inner: 1 } }} />)

    // Root's child (outer branch) should be visible at depth 0 expansion
    expect(screen.getByTestId('json-node-root.outer')).toBeInTheDocument()

    // But the branch's child (inner leaf) should not be visible initially
    expect(screen.queryByTestId('json-node-root.outer.inner')).not.toBeInTheDocument()

    // After clicking the toggle, it should become visible
    const toggle = screen.getByTestId('json-node-toggle-root.outer')
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    await user.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByTestId('json-node-root.outer.inner')).toBeInTheDocument()
  })

  it('renders leaf types correctly', () => {
    render(<JsonMessageTree value={{ s: 'hi', n: 42, b: true, z: null }} />)

    const strNode = screen.getByTestId('json-node-root.s')
    expect(strNode).toHaveTextContent('"hi"')

    const numNode = screen.getByTestId('json-node-root.n')
    expect(numNode).toHaveTextContent('42')

    const boolNode = screen.getByTestId('json-node-root.b')
    expect(boolNode).toHaveTextContent('true')

    const nullNode = screen.getByTestId('json-node-root.z')
    expect(nullNode).toHaveTextContent('null')
  })

  it('renders child cap indicator', () => {
    const obj = Object.fromEntries(Array.from({ length: 1001 }, (_, i) => [`k${i}`, i]))
    render(<JsonMessageTree value={obj} />)

    // Root is expanded by default, so the "(M more)" indicator should be visible
    expect(screen.getByTestId('json-node-more-root')).toBeInTheDocument()
    expect(screen.getByTestId('json-node-more-root')).toHaveTextContent(/1 more/)

    // Count rendered children (should be 1000, not 1001)
    const childRows = screen.getAllByTestId(/^json-node-root\.k/)
    expect(childRows).toHaveLength(1000)
  })

  it('copies tree with pretty-printed JSON', async () => {
    const user = userEvent.setup()
    render(<JsonMessageTree value={{ a: 1 }} />)

    const copyBtn = screen.getByTestId('json-message-copy')
    await user.click(copyBtn)

    expect(copyFn).toHaveBeenCalledWith(JSON.stringify({ a: 1 }, null, 2), 'message')
  })
})
