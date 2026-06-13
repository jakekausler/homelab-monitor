import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { HomeAssistantLogsTab } from './HomeAssistantLogsTab'

afterEach(() => {
  cleanup()
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
