import { useNavigate } from '@tanstack/react-router'
import { LogOut, User as UserIcon, SunMoon } from 'lucide-react'

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Button } from '@/components/ui/button'
import { useCurrentUser, useLogout } from '@/api/queries'

export function UserMenu({ onToggleTheme }: { onToggleTheme: () => void }) {
  const navigate = useNavigate()
  const { data } = useCurrentUser()
  const logout = useLogout()

  const username = data?.user.username ?? 'unknown'

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          aria-label="User menu"
          className="flex items-center gap-2"
        >
          <UserIcon className="size-4" />
          <span>{username}</span>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuLabel>Signed in as {username}</DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={onToggleTheme}>
          <SunMoon className="size-4" />
          Toggle theme
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          onSelect={() => {
            logout.mutate(undefined, {
              onSettled: () => {
                void navigate({ to: '/login' })
              },
            })
          }}
        >
          <LogOut className="size-4" />
          Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
