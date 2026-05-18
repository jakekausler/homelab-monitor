interface CrontabLineDiffProps {
  sourcePath: string
  oldLine: string
  newLine: string
}

/**
 * Presentational old→new crontab-line diff. Shared by InstallHeartbeatModal
 * and RemoveHeartbeatModal so the two render the diff identically.
 */
export function CrontabLineDiff({ sourcePath, oldLine, newLine }: CrontabLineDiffProps) {
  return (
    <div className="min-w-0">
      <h3 className="mb-2 text-sm font-medium">Crontab diff</h3>
      <div className="min-w-0 rounded-md bg-muted p-3 font-mono text-xs">
        <div className="mb-1 text-muted-foreground">File: {sourcePath}</div>
        <div className="mb-2 flex items-start gap-2">
          <span className="text-red-600 dark:text-red-400">-</span>
          <code className="break-all text-red-600 line-through dark:text-red-400">{oldLine}</code>
        </div>
        <div className="flex items-start gap-2">
          <span className="text-green-600 dark:text-green-400">+</span>
          <code className="break-all text-green-600 dark:text-green-400">{newLine}</code>
        </div>
      </div>
    </div>
  )
}
