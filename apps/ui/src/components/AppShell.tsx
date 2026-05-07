import { Outlet } from '@tanstack/react-router'
import { useCallback, useEffect, useState } from 'react'

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
  const [theme, setTheme] = useState<Theme>(readInitialTheme)

  useEffect(() => {
    const root = document.documentElement
    root.dataset.theme = theme
    root.style.colorScheme = theme
    window.localStorage.setItem(THEME_STORAGE_KEY, theme)
  }, [theme])

  const toggleTheme = useCallback(() => {
    setTheme((t) => (t === 'dark' ? 'light' : 'dark'))
  }, [])

  const toggleSidebar = useCallback(() => {
    setCollapsed((c) => !c)
  }, [])

  return (
    <div className="flex h-screen w-full bg-background text-foreground">
      <SidebarNav collapsed={collapsed} />
      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar onToggleSidebar={toggleSidebar} onToggleTheme={toggleTheme} />
        <main className="flex-1 overflow-auto p-6">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
