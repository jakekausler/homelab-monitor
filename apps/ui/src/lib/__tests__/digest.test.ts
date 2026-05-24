import { describe, expect, it } from 'vitest'

import { formatDigest } from '../digest'

describe('formatDigest', () => {
  it('returns em-dash for null', () => {
    expect(formatDigest(null)).toBe('—')
  })

  it('returns em-dash for undefined', () => {
    expect(formatDigest(undefined)).toBe('—')
  })

  it('returns em-dash for empty string', () => {
    expect(formatDigest('')).toBe('—')
  })

  it('truncates a full sha256 digest', () => {
    const full = 'sha256:c5dd3503828713c4949ae1bccd1d8d69f382c33d441954674a6b78ebe69c3331'
    expect(formatDigest(full)).toBe('sha256:c5dd35038287…')
  })

  it('returns short sha256 digest unchanged when hex ≤ 12 chars', () => {
    expect(formatDigest('sha256:abc123')).toBe('sha256:abc123')
  })

  it('returns non-digest strings unchanged (e.g., image refs)', () => {
    expect(formatDigest('nginx:1.27')).toBe('nginx:1.27')
    expect(formatDigest('prom/prometheus:v2.47.0')).toBe('prom/prometheus:v2.47.0')
    expect(formatDigest('ghcr.io/foo/bar:1.0')).toBe('ghcr.io/foo/bar:1.0')
  })

  it('case-insensitive hex match', () => {
    const full = 'sha256:ABC123DEF456789012345678901234567890ABCDEF1234567890ABCDEF1234567890'
    expect(formatDigest(full)).toBe('sha256:ABC123DEF456…')
  })
})
