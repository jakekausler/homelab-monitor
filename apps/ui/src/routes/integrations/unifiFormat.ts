/**
 * UniFi satisfaction is 0-100, but the controller emits -1 when "not computed".
 * Render that (and null/negative) as "—".
 */
export function formatSatisfaction(value: number | null | undefined): string {
  if (value === null || value === undefined || value < 0) {
    return '—'
  }
  return `${Math.round(value)}%`
}

/** Humanize a raw byte counter. No formatBytes util exists in the repo. */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) {
    return '—'
  }
  if (bytes < 1024) {
    return `${bytes} B`
  }
  const units = ['KiB', 'MiB', 'GiB', 'TiB', 'PiB']
  let value = bytes / 1024
  let unitIndex = 0
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024
    unitIndex += 1
  }
  return `${value.toFixed(1)} ${units[unitIndex]}`
}

/** Render a percent metric (cpu_pct / mem_pct) that may be null. */
export function formatPct(value: number | null | undefined): string {
  if (value === null || value === undefined) {
    return '—'
  }
  return `${Math.round(value)}%`
}

/** Render a temperature value (number | null). */
export function formatTemp(value: number | null | undefined): string {
  if (value === null || value === undefined) {
    return '—'
  }
  return `${Math.round(value)}°C`
}

/** Map a backend device kind string to a display label. Acronyms are uppercased; others are title-cased. */
export function formatDeviceKind(kind: string | null | undefined): string {
  if (kind === null || kind === undefined) return '—'
  const ACRONYMS: Record<string, string> = {
    ap: 'AP',
    pdu: 'PDU',
    udm: 'UDM',
  }
  const lower = kind.toLowerCase()
  if (lower in ACRONYMS) return ACRONYMS[lower]!
  return lower.charAt(0).toUpperCase() + lower.slice(1)
}

/** Map a backend WiFi band key to a display label (e.g. "2.4ghz" → "2.4 GHz"). */
export function formatBand(key: string): string {
  const BANDS: Record<string, string> = {
    '2.4ghz': '2.4 GHz',
    '5ghz': '5 GHz',
    '6ghz': '6 GHz',
  }
  if (key in BANDS) return BANDS[key]!
  // Fallback: if key ends with "ghz", treat the prefix as the numeric part.
  if (key.toLowerCase().endsWith('ghz')) {
    return key.slice(0, key.length - 3) + ' GHz'
  }
  return key.charAt(0).toUpperCase() + key.slice(1)
}

/** Map a backend WiFi link type key to a display label (e.g. "wired" → "Wired"). */
export function formatLink(key: string): string {
  const LINKS: Record<string, string> = {
    wired: 'Wired',
    wireless: 'Wireless',
  }
  if (key in LINKS) return LINKS[key]!
  return key.charAt(0).toUpperCase() + key.slice(1)
}

/** Render a bits-per-second value using decimal (÷1000) bit-rate units. */
export function formatBitrate(bps: number | null | undefined): string {
  if (bps === null || bps === undefined) return '—'
  if (bps === 0) return '0 bps'

  // Tiers in descending order. We try each tier; if rounding would produce ≥1000
  // in this tier, we promote to the next higher one.
  const tiers: [number, string][] = [
    [1_000_000_000_000, 'Tbps'],
    [1_000_000_000, 'Gbps'],
    [1_000_000, 'Mbps'],
    [1_000, 'Kbps'],
  ]

  for (let i = tiers.length - 1; i >= 0; i--) {
    const [divisor, label] = tiers[i]!
    const rounded = +(bps / divisor).toFixed(2)
    // Only use this tier if the value is ≥1 in this unit AND rounding doesn't overshoot 1000
    if (rounded >= 1 && rounded < 1000) {
      return `${rounded} ${label}`
    }
  }

  // Overshoot case: values that round to >= 1000 Tbps fall through the loop.
  const tbps = +(bps / 1_000_000_000_000).toFixed(2)
  if (tbps >= 1) return `${tbps} Tbps`

  return `${bps} bps`
}
