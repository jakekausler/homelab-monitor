interface TimezoneToggleProps {
  /** True when the current preference is UTC. */
  checked: boolean
  /** Flip the preference (local <-> utc). */
  onChange: () => void
  id?: string
}

/**
 * UTC/local toggle for log timestamps. Cloned from <WrapToggle> so it sits
 * next to it consistently. Unchecked = local (the configured display zone);
 * checked = UTC. STAGE-004-009.
 */
export function TimezoneToggle({ checked, onChange, id }: TimezoneToggleProps) {
  return (
    <label
      className="flex items-center gap-1.5 text-xs text-muted-foreground"
      data-testid="timezone-toggle"
      htmlFor={id}
    >
      <input
        id={id}
        type="checkbox"
        className="size-3.5 rounded border-input accent-foreground"
        checked={checked}
        onChange={() => {
          onChange()
        }}
      />
      UTC
    </label>
  )
}
