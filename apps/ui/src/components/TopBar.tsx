import { Bell, Menu, Search } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { UserMenu } from '@/components/UserMenu'

export function TopBar({
  onToggleSidebar,
  onToggleMobile,
  onToggleTheme,
}: {
  onToggleSidebar: () => void
  onToggleMobile: () => void
  onToggleTheme: () => void
}) {
  return (
    <header className="flex h-14 items-center gap-3 border-b border-border bg-background px-4">
      {/* Mobile hamburger — opens overlay */}
      <Button
        variant="ghost"
        size="icon"
        aria-label="Open navigation menu"
        className="md:hidden"
        onClick={onToggleMobile}
      >
        <Menu className="size-4" />
      </Button>
      {/* Desktop hamburger — collapses/expands persistent sidebar */}
      <Button
        variant="ghost"
        size="icon"
        aria-label="Toggle sidebar"
        className="hidden md:inline-flex"
        onClick={onToggleSidebar}
      >
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
      <Tooltip>
        <TooltipTrigger asChild>
          <span tabIndex={0}>
            <Button variant="ghost" size="icon" aria-label="Notifications (coming soon)" disabled>
              <Bell className="size-4" />
            </Button>
          </span>
        </TooltipTrigger>
        <TooltipContent side="bottom">Coming soon</TooltipContent>
      </Tooltip>
      <UserMenu onToggleTheme={onToggleTheme} />
    </header>
  )
}
