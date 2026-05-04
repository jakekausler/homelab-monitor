import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { App } from './App'

describe('App', () => {
  it('renders the homelab-monitor heading', () => {
    render(<App />)
    expect(screen.getByText('homelab-monitor')).toBeInTheDocument()
    expect(screen.getByText(/EPIC-001 STAGE-001-002/)).toBeInTheDocument()
    expect(screen.getByText('dev')).toBeInTheDocument()
  })
})
