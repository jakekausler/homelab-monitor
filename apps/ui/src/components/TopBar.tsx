import { Bell, Menu, Search } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { UserMenu } from '@/components/UserMenu'

export function TopBar({
  onToggleSidebar,
  onToggleTheme,
}: {
  onToggleSidebar: () => void
  onToggleTheme: () => void
}) {
  return (
    <header className="flex h-14 items-center gap-3 border-b border-border bg-background px-4">
      <Button variant="ghost" size="icon" aria-label="Toggle sidebar" onClick={onToggleSidebar}>
        <Menu className="size-4" />
      </Button>
      <div className="relative flex-1 max-w-md">
        <Search className="pointer-events-none absolute left-2 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          type="search"
          placeholder="Search (coming soon)"
          aria-label="Search"
          disabled
          className="pl-8"
        />
      </div>
      <Button variant="ghost" size="icon" aria-label="Notifications" className="relative">
        <Bell className="size-4" />
        <span
          aria-hidden
          className="absolute right-2 top-2 size-2 rounded-full bg-status-warning"
        />
      </Button>
      <UserMenu onToggleTheme={onToggleTheme} />
    </header>
  )
}
