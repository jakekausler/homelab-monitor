type BadgeVariant = 'ok' | 'warn' | 'critical' | 'muted'

/**
 * Camera connection tone: connected -> ok "Connected", disconnected ->
 * critical "Disconnected".
 */
export function cameraConnectedBadge(connected: boolean): {
  variant: BadgeVariant
  label: string
} {
  return connected
    ? { variant: 'ok', label: 'Connected' }
    : { variant: 'critical', label: 'Disconnected' }
}
