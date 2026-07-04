import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import { FeedbackList } from '../FeedbackList'
import type { RunFeedback } from '@/api/autofix-runs'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

function makeFeedback(overrides: Partial<RunFeedback> = {}): RunFeedback {
  return {
    id: 'fb-1',
    created_at: '2026-07-03T00:00:00Z',
    kind: 'other',
    runbook_run_id: 'run-1',
    structured_hint: null,
    suggestion_text: 'Some suggestion',
    ...overrides,
  }
}

const ALL_KINDS: RunFeedback['kind'][] = [
  'missing_capability',
  'config_change',
  'runbook_gap',
  'blocked',
  'worked_around',
  'other',
  'parse_error',
]

describe('FeedbackList', () => {
  it('renders empty-state testid and text when there are 0 items', () => {
    render(<FeedbackList items={[]} />)
    expect(screen.getByTestId('feedback-empty')).toHaveTextContent(
      'No feedback items for this run.',
    )
  })

  it.each(ALL_KINDS)('renders a badge for feedback kind "%s"', (kind) => {
    render(<FeedbackList items={[makeFeedback({ id: `fb-${kind}`, kind })]} />)
    expect(screen.getByTestId(`feedback-kind-${kind}`)).toHaveTextContent(kind)
  })

  it('parse_error kind gets the "critical" badge variant, distinct from other kinds', () => {
    render(<FeedbackList items={[makeFeedback({ id: 'fb-pe', kind: 'parse_error' })]} />)
    const badge = screen.getByTestId('feedback-kind-parse_error')
    // Badge component applies variant-specific classes; critical variant should
    // differ from the default "outline" variant used by non-parse_error kinds.
    render(<FeedbackList items={[makeFeedback({ id: 'fb-other', kind: 'other' })]} />)
    const otherBadge = screen.getByTestId('feedback-kind-other')
    expect(badge.className).not.toBe(otherBadge.className)
  })

  it('expand/collapse toggle for structured_hint changes button text and shows pretty-printed JSON', () => {
    render(
      <FeedbackList
        items={[
          makeFeedback({
            id: 'fb-hint',
            structured_hint: { missing_tool: 'curl', reason: 'not installed' },
          }),
        ]}
      />,
    )
    const toggle = screen.getByTestId('feedback-hint-toggle-fb-hint')
    expect(toggle).toHaveTextContent('Show structured hint')
    expect(screen.queryByTestId('feedback-hint-body-fb-hint')).not.toBeInTheDocument()

    fireEvent.click(toggle)
    expect(toggle).toHaveTextContent('Hide structured hint')
    const body = screen.getByTestId('feedback-hint-body-fb-hint')
    expect(body.textContent).toContain('"missing_tool": "curl"')

    fireEvent.click(toggle)
    expect(toggle).toHaveTextContent('Show structured hint')
    expect(screen.queryByTestId('feedback-hint-body-fb-hint')).not.toBeInTheDocument()
  })

  it('does not render a toggle when structured_hint is null', () => {
    render(<FeedbackList items={[makeFeedback({ id: 'fb-nohint', structured_hint: null })]} />)
    expect(screen.queryByTestId('feedback-hint-toggle-fb-nohint')).not.toBeInTheDocument()
  })

  it('renders each item with its own card testid', () => {
    render(<FeedbackList items={[makeFeedback({ id: 'fb-a' }), makeFeedback({ id: 'fb-b' })]} />)
    expect(screen.getByTestId('feedback-item-fb-a')).toBeInTheDocument()
    expect(screen.getByTestId('feedback-item-fb-b')).toBeInTheDocument()
  })
})
