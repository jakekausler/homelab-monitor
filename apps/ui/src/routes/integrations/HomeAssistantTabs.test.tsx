import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { HomeAssistantLogsTab } from './HomeAssistantLogsTab'
import { HomeAssistantStatusTab } from './HomeAssistantStatusTab'

afterEach(() => {
  cleanup()
})

describe('HomeAssistantStatusTab', () => {
  it('renders Updates section title', () => {
    render(<HomeAssistantStatusTab />)
    expect(screen.getByRole('heading', { name: /updates/i })).toBeInTheDocument()
  })

  it('renders Integration status section title', () => {
    render(<HomeAssistantStatusTab />)
    expect(screen.getByRole('heading', { name: /integration status/i })).toBeInTheDocument()
  })

  it('renders Updates placeholder copy', () => {
    render(<HomeAssistantStatusTab />)
    expect(screen.getByText('Available updates will appear here.')).toBeInTheDocument()
  })

  it('renders Integration status placeholder copy', () => {
    render(<HomeAssistantStatusTab />)
    expect(screen.getByText('Integration status will appear here.')).toBeInTheDocument()
  })
})

describe('HomeAssistantLogsTab', () => {
  it('renders Logs section title', () => {
    render(<HomeAssistantLogsTab />)
    expect(screen.getByRole('heading', { name: /^logs$/i })).toBeInTheDocument()
  })

  it('renders Logs placeholder copy', () => {
    render(<HomeAssistantLogsTab />)
    expect(screen.getByText('Recent Home Assistant logs will appear here.')).toBeInTheDocument()
  })
})
