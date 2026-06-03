import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { copyToClipboard } from '../clipboard'

describe('clipboard', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    Object.defineProperty(document, 'execCommand', {
      value: vi.fn<(commandId: string, showUI?: boolean, value?: string) => boolean>(),
      configurable: true,
      writable: true,
    })
  })

  afterEach(() => {
    // Remove execCommand so each test starts from a clean jsdom state
    Object.defineProperty(document, 'execCommand', {
      value: undefined,
      configurable: true,
      writable: true,
    })
  })

  describe('copyToClipboard', () => {
    it('uses navigator.clipboard.writeText when available and succeeds', async () => {
      const writeTextMock = vi.fn().mockResolvedValue(undefined)
      // Mock the navigator.clipboard API
      Object.defineProperty(navigator, 'clipboard', {
        value: { writeText: writeTextMock },
        configurable: true,
      })

      const result = await copyToClipboard('test text')

      expect(result).toBe(true)
      expect(writeTextMock).toHaveBeenCalledWith('test text')
    })

    it('falls back to execCommand when clipboard.writeText rejects', async () => {
      const writeTextMock = vi.fn().mockRejectedValue(new Error('denied'))
      Object.defineProperty(navigator, 'clipboard', {
        value: { writeText: writeTextMock },
        configurable: true,
      })

      const execCommandSpy = vi.spyOn(document, 'execCommand').mockReturnValue(true)
      const textareaMock = document.createElement('textarea')
      vi.spyOn(document, 'createElement').mockReturnValue(textareaMock)
      vi.spyOn(document.body, 'removeChild').mockImplementation(() => textareaMock)

      const result = await copyToClipboard('test text')

      expect(result).toBe(true)
      expect(execCommandSpy).toHaveBeenCalledWith('copy')
    })

    it('uses execCommand path when navigator.clipboard is undefined', async () => {
      Object.defineProperty(navigator, 'clipboard', {
        value: undefined,
        configurable: true,
      })

      const execCommandSpy = vi.spyOn(document, 'execCommand').mockReturnValue(true)
      const textareaMock = document.createElement('textarea')
      vi.spyOn(document, 'createElement').mockReturnValue(textareaMock)
      vi.spyOn(document.body, 'removeChild').mockImplementation(() => textareaMock)

      const result = await copyToClipboard('test text')

      expect(result).toBe(true)
      expect(execCommandSpy).toHaveBeenCalledWith('copy')
    })

    it('returns false when execCommand returns false', async () => {
      Object.defineProperty(navigator, 'clipboard', {
        value: undefined,
        configurable: true,
      })

      vi.spyOn(document, 'execCommand').mockReturnValue(false)
      const textareaMock = document.createElement('textarea')
      vi.spyOn(document, 'createElement').mockReturnValue(textareaMock)
      vi.spyOn(document.body, 'removeChild').mockImplementation(() => textareaMock)

      const result = await copyToClipboard('test text')

      expect(result).toBe(false)
    })

    it('returns false when execCommand throws', async () => {
      Object.defineProperty(navigator, 'clipboard', {
        value: undefined,
        configurable: true,
      })

      vi.spyOn(document, 'execCommand').mockImplementation(() => {
        throw new Error('exec error')
      })
      const textareaMock = document.createElement('textarea')
      vi.spyOn(document, 'createElement').mockReturnValue(textareaMock)
      vi.spyOn(document.body, 'removeChild').mockImplementation(() => textareaMock)

      const result = await copyToClipboard('test text')

      expect(result).toBe(false)
    })
  })
})
