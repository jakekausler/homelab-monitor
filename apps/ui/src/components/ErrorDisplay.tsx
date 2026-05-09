/**
 * Shared error display used by both the root route's `errorComponent` and
 * the router's `defaultErrorComponent`. Safely coerces unknown error
 * shapes to a string so a raw HttpProblem object (`{code, message,
 * details}`) cannot reach React as a child (which would trigger error
 * #31 — "Objects are not valid as a React child").
 */
export function ErrorDisplay({ error }: { error: unknown }) {
  const message =
    error instanceof Error
      ? error.message
      : typeof error === 'object' && error !== null && 'message' in error
        ? String(error.message)
        : 'An unexpected error occurred'
  return (
    <div role="alert" className="p-6 text-destructive">
      <p className="font-semibold">Internal error</p>
      <p className="text-sm text-muted-foreground">{message}</p>
    </div>
  )
}
