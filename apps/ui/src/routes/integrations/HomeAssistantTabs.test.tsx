import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { HomeAssistantHealthTab } from './HomeAssistantHealthTab'
import { HomeAssistantLogsTab } from './HomeAssistantLogsTab'
import { HomeAssistantStatusTab } from './HomeAssistantStatusTab'

afterEach(() => {
  cleanup()
})

describe('HomeAssistantHealthTab', () => {
  it('renders Entity health section title', () => {
    render(<HomeAssistantHealthTab />)
    expect(screen.getByRole('heading', { name: /entity health/i })).toBeInTheDocument()
  })

  it('renders Battery section title', () => {
    render(<HomeAssistantHealthTab />)
    expect(screen.getByRole('heading', { name: /battery/i })).toBeInTheDocument()
  })

  it('renders Entity health placeholder copy', () => {
    render(<HomeAssistantHealthTab />)
    expect(screen.getByText('Entity health will appear here.')).toBeInTheDocument()
  })

  it('renders Battery placeholder copy', () => {
    render(<HomeAssistantHealthTab />)
    expect(screen.getByText('Battery status will appear here.')).toBeInTheDocument()
  })
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
