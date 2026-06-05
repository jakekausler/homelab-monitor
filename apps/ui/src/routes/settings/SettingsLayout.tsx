import { Outlet } from '@tanstack/react-router'

export function SettingsLayout() {
  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Settings</h1>
        <p className="text-sm text-muted-foreground">
          Logs retention and disk budget today; more settings land in upcoming epics.
        </p>
      </div>
      <Outlet />
    </div>
  )
}
