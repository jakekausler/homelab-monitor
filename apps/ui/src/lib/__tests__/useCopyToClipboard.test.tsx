import { describe, it, expect, vi, afterEach } from 'vitest'
import { renderHook, act, cleanup } from '@testing-library/react'
import { useCopyToClipboard } from '../useCopyToClipboard'

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}))

vi.mock('../clipboard', () => ({
  copyToClipboard: vi.fn(),
}))

import { toast } from 'sonner'
import { copyToClipboard } from '../clipboard'

describe('useCopyToClipboard', () => {
  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('calls toast.success("Copied") when copy succeeds without label', async () => {
    vi.mocked(copyToClipboard).mockResolvedValue(true)

    const { result } = renderHook(() => useCopyToClipboard())

    await act(async () => {
      await result.current('test text')
    })

    expect(toast.success).toHaveBeenCalledWith('Copied')
    expect(toast.error).not.toHaveBeenCalled()
  })

  it('calls toast.success("<label> copied") when copy succeeds with label', async () => {
    vi.mocked(copyToClipboard).mockResolvedValue(true)

    const { result } = renderHook(() => useCopyToClipboard())

    await act(async () => {
      await result.current('test text', 'Service')
    })

    expect(toast.success).toHaveBeenCalledWith('Service copied')
    expect(toast.error).not.toHaveBeenCalled()
  })

  it('calls toast.error("Copy failed") when copy fails', async () => {
    vi.mocked(copyToClipboard).mockResolvedValue(false)

    const { result } = renderHook(() => useCopyToClipboard())

    await act(async () => {
      await result.current('test text')
    })

    expect(toast.error).toHaveBeenCalledWith('Copy failed')
    expect(toast.success).not.toHaveBeenCalled()
  })

  it('passes text to copyToClipboard', async () => {
    vi.mocked(copyToClipboard).mockResolvedValue(true)

    const { result } = renderHook(() => useCopyToClipboard())

    await act(async () => {
      await result.current('my test content')
    })

    expect(copyToClipboard).toHaveBeenCalledWith('my test content')
  })
})
