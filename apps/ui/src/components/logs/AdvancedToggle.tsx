interface AdvancedToggleProps {
  checked: boolean
  onChange: (next: boolean) => void
  id?: string
}

/**
 * "Advanced (LogsQL)" toggle. Cloned from <WrapToggle> so it sits consistently
 * in the Logs Explorer header. Checked = advanced (raw LogsQL editor);
 * unchecked = plain-text search. STAGE-004-011.
 */
export function AdvancedToggle({ checked, onChange, id }: AdvancedToggleProps) {
  return (
    <label
      className="flex items-center gap-1.5 text-xs text-muted-foreground"
      data-testid="logs-advanced-toggle"
      htmlFor={id}
    >
      <input
        id={id}
        type="checkbox"
        className="size-3.5 rounded border-input accent-foreground"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
      />
      Advanced (LogsQL)
    </label>
  )
}
