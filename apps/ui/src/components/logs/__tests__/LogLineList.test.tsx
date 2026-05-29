import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { LogLineList } from '@/components/logs/LogLineList'
import { severityTintClass } from '@/components/logs/severity'
import type { LogLine } from '@/components/logs/types'

afterEach(cleanup)

function line(message: string, severity: string | null = null): LogLine {
  return {
    timestamp: '2026-05-21T14:30:00Z',
    message,
    stream: 'stdout',
    severity,
    host: null,
    service: null,
    fields: {},
  }
}

describe('LogLineList', () => {
  it('renders lines', () => {
    render(<LogLineList lines={[line('a'), line('b')]} testId="x" />)

    const body = screen.getByTestId('x')
    expect(body.textContent).toContain('a')
    expect(body.textContent).toContain('b')
    expect(body.textContent).toContain('2026-05-21 14:30:00 UTC')
  })

  it('renders empty with emptyContent', () => {
    render(<LogLineList lines={[]} testId="x" emptyContent={<span>nope</span>} />)

    expect(screen.getByTestId('x')).toBeInTheDocument()
    expect(screen.getByText('nope')).toBeInTheDocument()
  })

  it('renders empty without emptyContent', () => {
    render(<LogLineList lines={[]} testId="x" />)

    const body = screen.getByTestId('x')
    expect(body).toBeInTheDocument()
    expect(body.textContent).toBe('')
  })

  it('applies severity tint red for error', () => {
    render(<LogLineList lines={[line('e', 'error')]} testId="x" />)

    const body = screen.getByTestId('x')
    const redDiv = body.querySelector('.text-red-500')
    expect(redDiv).toBeInTheDocument()
    expect(redDiv).toHaveTextContent('e')
  })

  it('applies severity tint yellow for warn', () => {
    render(<LogLineList lines={[line('w', 'warn')]} testId="x" />)

    const body = screen.getByTestId('x')
    const yellowDiv = body.querySelector('.text-yellow-500')
    expect(yellowDiv).toBeInTheDocument()
    expect(yellowDiv).toHaveTextContent('w')
  })

  it('does not tint for info severity', () => {
    render(<LogLineList lines={[line('i', 'info')]} testId="x" />)

    const body = screen.getByTestId('x')
    expect(body.querySelector('.text-red-500')).not.toBeInTheDocument()
    expect(body.querySelector('.text-yellow-500')).not.toBeInTheDocument()
  })

  it('does not tint for null severity', () => {
    render(<LogLineList lines={[line('n', null)]} testId="x" />)

    const body = screen.getByTestId('x')
    expect(body.querySelector('.text-red-500')).not.toBeInTheDocument()
    expect(body.querySelector('.text-yellow-500')).not.toBeInTheDocument()
  })

  describe('severityTintClass', () => {
    it('returns text-red-500 for error', () => {
      expect(severityTintClass('error')).toBe('text-red-500')
    })

    it('returns text-red-500 for critical', () => {
      expect(severityTintClass('critical')).toBe('text-red-500')
    })

    it('returns text-red-500 for alert', () => {
      expect(severityTintClass('alert')).toBe('text-red-500')
    })

    it('returns text-red-500 for emergency', () => {
      expect(severityTintClass('emergency')).toBe('text-red-500')
    })

    it('returns text-yellow-500 for warn', () => {
      expect(severityTintClass('warn')).toBe('text-yellow-500')
    })

    it('returns empty string for info', () => {
      expect(severityTintClass('info')).toBe('')
    })

    it('returns empty string for null', () => {
      expect(severityTintClass(null)).toBe('')
    })

    it('returns empty string for undefined', () => {
      expect(severityTintClass(undefined)).toBe('')
    })
  })
})
