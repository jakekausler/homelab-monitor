import type { QueryClient } from '@tanstack/react-query'
import { Outlet, createRootRouteWithContext } from '@tanstack/react-router'
import { Toaster } from 'sonner'
import { ErrorDisplay } from '@/components/ErrorDisplay'
import { TooltipProvider } from '@/components/ui/tooltip'

export interface RouterContext {
  queryClient: QueryClient
}

export const Route = createRootRouteWithContext<RouterContext>()({
  component: () => (
    <TooltipProvider>
      <Toaster position="top-right" richColors />
      <Outlet />
    </TooltipProvider>
  ),
  errorComponent: ErrorDisplay,
})
