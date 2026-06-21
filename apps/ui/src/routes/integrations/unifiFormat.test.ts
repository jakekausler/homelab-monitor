import { describe, expect, it } from 'vitest'

import {
  formatBand,
  formatBitrate,
  formatBytes,
  formatDeviceKind,
  formatLink,
  formatPct,
  formatSatisfaction,
  formatTemp,
} from './unifiFormat'

describe('unifiFormat', () => {
  it('renders sentinel satisfaction as em-dash', () => {
    expect(formatSatisfaction(-1)).toBe('—')
    expect(formatSatisfaction(null)).toBe('—')
    expect(formatSatisfaction(undefined)).toBe('—')
  })
  it('renders real satisfaction as percent', () => {
    expect(formatSatisfaction(87)).toBe('87%')
  })
  it('humanizes bytes', () => {
    expect(formatBytes(0)).toBe('0 B')
    expect(formatBytes(1536)).toBe('1.5 KiB')
    expect(formatBytes(null)).toBe('—')
  })
  it('renders null metrics as dash', () => {
    expect(formatPct(null)).toBe('—')
    expect(formatTemp(null)).toBe('—')
  })
  it('formats bitrate in human units', () => {
    expect(formatBitrate(null)).toBe('—')
    expect(formatBitrate(undefined)).toBe('—')
    expect(formatBitrate(0)).toBe('0 bps')
    expect(formatBitrate(1_000_000_000)).toBe('1 Gbps')
    expect(formatBitrate(100_000_000)).toBe('100 Mbps')
    expect(formatBitrate(10_000_000)).toBe('10 Mbps')
    expect(formatBitrate(2_500_000_000)).toBe('2.5 Gbps')
    expect(formatBitrate(999_999)).toBe('1 Mbps')
  })
  it('maps device kinds to display labels', () => {
    expect(formatDeviceKind('ap')).toBe('AP')
    expect(formatDeviceKind('pdu')).toBe('PDU')
    expect(formatDeviceKind('gateway')).toBe('Gateway')
    expect(formatDeviceKind('switch')).toBe('Switch')
    expect(formatDeviceKind(null)).toBe('—')
    expect(formatDeviceKind(undefined)).toBe('—')
  })
  it('formats WiFi band keys', () => {
    expect(formatBand('2.4ghz')).toBe('2.4 GHz')
    expect(formatBand('5ghz')).toBe('5 GHz')
    expect(formatBand('6ghz')).toBe('6 GHz')
    // fallback: unknown key ending in "ghz"
    expect(formatBand('60ghz')).toBe('60 GHz')
    // fallback: unknown key not ending in "ghz"
    expect(formatBand('unknown')).toBe('Unknown')
  })
  it('formats WiFi link type keys', () => {
    expect(formatLink('wired')).toBe('Wired')
    expect(formatLink('wireless')).toBe('Wireless')
    // fallback: unknown key
    expect(formatLink('fiber')).toBe('Fiber')
    expect(formatLink('')).toBe('')
  })
})
