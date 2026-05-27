import { ContainerList } from './ContainerList'

export function DockerIntegrationPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Docker integration</h1>
        <p className="text-sm text-muted-foreground">Container inventory, health, and actions.</p>
      </div>
      <ContainerList />
    </div>
  )
}
