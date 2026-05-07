import { Outlet } from '@tanstack/react-router'
import { useCallback, useEffect, useState } from 'react'
import { X } from 'lucide-react'

import { SidebarNav } from '@/components/SidebarNav'
import { TopBar } from '@/components/TopBar'

const THEME_STORAGE_KEY = 'homelab-monitor:theme'
type Theme = 'dark' | 'light'

function readInitialTheme(): Theme {
  if (typeof window === 'undefined') return 'dark'
  const stored = window.localStorage.getItem(THEME_STORAGE_KEY)
  return stored === 'light' ? 'light' : 'dark'
}

export function AppShell() {
  const [collapsed, setCollapsed] = useState(false)
  const [mobileOpen, setMobileOpen] = useState(false)
  const [theme, setTheme] = useState<Theme>(readInitialTheme)

  // Theme effect — unchanged
  useEffect(() => {
    const root = document.documentElement
    root.dataset.theme = theme
    root.style.colorScheme = theme
    window.localStorage.setItem(THEME_STORAGE_KEY, theme)
  }, [theme])

  // Close mobile sidebar when resizing to desktop
  useEffect(() => {
    const mq = window.matchMedia('(min-width: 768px)')
    const handler = (e: MediaQueryListEvent) => {
      if (e.matches) setMobileOpen(false)
    }
    mq.addEventListener('change', handler)
    return () => mq.removeEventListener('change', handler)
  }, [])

  // Body scroll-lock while mobile sidebar is open
  useEffect(() => {
    document.body.style.overflow = mobileOpen ? 'hidden' : ''
    return () => {
      document.body.style.overflow = ''
    }
  }, [mobileOpen])

  const toggleTheme = useCallback(() => {
    setTheme((t) => (t === 'dark' ? 'light' : 'dark'))
  }, [])

  const toggleSidebar = useCallback(() => {
    setCollapsed((c) => !c)
  }, [])

  const toggleMobile = useCallback(() => {
    setMobileOpen((o) => !o)
  }, [])

  const closeMobile = useCallback(() => {
    setMobileOpen(false)
  }, [])

  return (
    <div className="flex h-screen w-full bg-background text-foreground">
      {/* Desktop sidebar — hidden on mobile */}
      <div className="hidden md:flex">
        <SidebarNav collapsed={collapsed} />
      </div>

      {/* Mobile overlay — only rendered when open, hidden on desktop */}
      {mobileOpen && (
        <>
          {/* Backdrop */}
          <div
            className="fixed inset-0 z-40 bg-black/50 md:hidden"
            onClick={closeMobile}
            aria-hidden="true"
          />
          {/* Sheet */}
          <aside
            className="fixed inset-y-0 left-0 z-50 flex w-full max-w-xs flex-col bg-sidebar md:hidden"
            role="dialog"
            aria-modal="true"
            aria-label="Navigation menu"
          >
            <div className="flex items-center justify-between border-b border-border px-3 py-2">
              <span className="text-sm font-semibold text-foreground">homelab-monitor</span>
              <button
                type="button"
                onClick={closeMobile}
                aria-label="Close menu"
                className="rounded-md p-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground"
              >
                <X className="size-5" />
              </button>
            </div>
            <SidebarNav collapsed={false} onNavigate={closeMobile} />
          </aside>
        </>
      )}

      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar
          onToggleSidebar={toggleSidebar}
          onToggleMobile={toggleMobile}
          onToggleTheme={toggleTheme}
        />
        <main className="flex-1 overflow-auto p-6">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
