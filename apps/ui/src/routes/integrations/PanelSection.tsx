import type { ReactNode } from 'react'
import { Card, CardContent } from '@/components/ui/card'

interface PanelSectionProps {
  title: string
  children: ReactNode
}

export function PanelSection({ title, children }: PanelSectionProps) {
  return (
    <section>
      <Card>
        <CardContent className="p-4">
          <h2 className="mb-3 text-sm font-medium">{title}</h2>
          {children}
        </CardContent>
      </Card>
    </section>
  )
}
