import { Link } from '@tanstack/react-router'
import {
  AlertTriangle,
  Boxes,
  Cable,
  CalendarRange,
  ClipboardList,
  Cog,
  FileText,
  Gauge,
  Layout,
  ScrollText,
  ShieldCheck,
  Sparkles,
  Wand2,
} from 'lucide-react'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'

interface NavItem {
  label: string
  to?: string
  icon: typeof Layout
  disabledNote?: string
}

const NAV_ITEMS: NavItem[] = [
  { label: 'Overview', to: '/overview', icon: Layout },
  { label: 'Alerts', to: '/alerts', icon: AlertTriangle },
  { label: 'Inventory', icon: Boxes, disabledNote: 'Coming soon' },
  { label: 'Integrations', icon: Cable, disabledNote: 'Coming soon' },
  { label: 'Logs', icon: ScrollText, disabledNote: 'Coming soon' },
  { label: 'Metrics', to: '/metrics', icon: Gauge },
  { label: 'Runbooks', icon: FileText, disabledNote: 'Coming soon' },
  { label: 'Auto-fix history', icon: Wand2, disabledNote: 'Coming soon' },
  {
    label: 'Discovery & suggestions',
    icon: Sparkles,
    disabledNote: 'Coming soon',
  },
  { label: 'Tool analysis', icon: ClipboardList, disabledNote: 'Coming soon' },
  {
    label: 'Maintenance windows',
    icon: CalendarRange,
    disabledNote: 'Coming soon',
  },
  { label: 'Self-status', icon: ShieldCheck, disabledNote: 'Coming soon' },
  { label: 'Settings', icon: Cog, disabledNote: 'Coming soon' },
]

export function SidebarNav({
  collapsed,
  onNavigate,
  ariaLabel = 'Primary',
}: {
  collapsed: boolean
  onNavigate?: () => void
  ariaLabel?: string
}) {
  return (
    <nav
      aria-label={ariaLabel}
      className={cn(
        'flex h-full flex-col gap-1 border-r border-border bg-sidebar p-2 transition-[width]',
        collapsed ? 'w-14' : 'w-64',
      )}
    >
      <div className="px-2 py-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        {collapsed ? null : 'homelab-monitor'}
      </div>
      {NAV_ITEMS.map((item) => {
        const Icon = item.icon
        if (item.to !== undefined) {
          return (
            <Link
              key={item.label}
              to={item.to}
              onClick={onNavigate}
              activeProps={{
                className: 'bg-accent text-accent-foreground',
              }}
              className={cn(
                'flex items-center gap-3 rounded-md px-2 py-2 text-sm text-foreground hover:bg-accent hover:text-accent-foreground',
                collapsed && 'justify-center',
              )}
            >
              <Icon className="size-4 shrink-0" />
              {collapsed ? null : <span>{item.label}</span>}
            </Link>
          )
        }
        return (
          <Tooltip key={item.label}>
            <TooltipTrigger asChild>
              <button
                type="button"
                aria-disabled
                disabled
                className={cn(
                  'flex cursor-not-allowed items-center gap-3 rounded-md px-2 py-2 text-sm text-muted-foreground opacity-60',
                  collapsed && 'justify-center',
                )}
                data-tooltip={item.disabledNote ?? 'Coming soon'}
              >
                <Icon className="size-4 shrink-0" />
                {collapsed ? null : <span>{item.label}</span>}
              </button>
            </TooltipTrigger>
            <TooltipContent side="right">{item.disabledNote ?? 'Coming soon'}</TooltipContent>
          </Tooltip>
        )
      })}
    </nav>
  )
}
