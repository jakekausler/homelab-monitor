import type { QueryClient } from '@tanstack/react-query'
import { Outlet, createRootRouteWithContext } from '@tanstack/react-router'
import { TooltipProvider } from '@/components/ui/tooltip'

interface RouterContext {
  queryClient: QueryClient
}

export const Route = createRootRouteWithContext<RouterContext>()({
  component: () => (
    <TooltipProvider>
      <Outlet />
    </TooltipProvider>
  ),
})
