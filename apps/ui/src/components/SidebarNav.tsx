import { Link } from '@tanstack/react-router'
import {
  AlertTriangle,
  Boxes,
  Cable,
  CalendarRange,
  ClipboardList,
  Cog,
  Container,
  FileText,
  Gauge,
  HousePlug,
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
  isSectionLabel?: boolean
  indent?: boolean
}

const NAV_ITEMS: NavItem[] = [
  { label: 'Overview', to: '/overview', icon: Layout },
  { label: 'Alerts', to: '/alerts', icon: AlertTriangle },
  { label: 'Inventory', to: '/inventory/crons', icon: Boxes },
  { label: 'Integrations', icon: Cable, isSectionLabel: true },
  { label: 'Docker', to: '/integrations/docker', icon: Container, indent: true },
  { label: 'Home Assistant', to: '/integrations/home-assistant', icon: HousePlug, indent: true },
  { label: 'Logs', to: '/logs', icon: ScrollText },
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
  { label: 'Settings', to: '/settings', icon: Cog },
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
        {collapsed ? null : 'Homelab Monitor'}
      </div>
      {NAV_ITEMS.map((item) => {
        const Icon = item.icon
        if (item.isSectionLabel) {
          return (
            <div
              key={item.label}
              className={cn(
                'px-2 pt-3 pb-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground',
                collapsed && 'hidden',
              )}
              role="heading"
              aria-level={2}
            >
              {collapsed ? (
                <Icon aria-hidden="true" className="size-4 shrink-0 mx-auto" />
              ) : (
                <span className="flex items-center gap-3">
                  <Icon aria-hidden="true" className="size-4 shrink-0" />
                  {item.label}
                </span>
              )}
            </div>
          )
        }
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
                item.indent && 'pl-6',
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
