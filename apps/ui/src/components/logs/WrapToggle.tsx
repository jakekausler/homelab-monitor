interface WrapToggleProps {
  checked: boolean
  onChange: (next: boolean) => void
  id?: string
}

export function WrapToggle({ checked, onChange, id }: WrapToggleProps) {
  return (
    <label
      className="flex items-center gap-1.5 text-xs text-muted-foreground"
      data-testid="wrap-toggle"
      htmlFor={id}
    >
      <input
        id={id}
        type="checkbox"
        className="size-3.5 rounded border-input accent-foreground"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
      />
      Wrap
    </label>
  )
}
