import { useEffect } from 'react'
import { useNavigate } from '@tanstack/react-router'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'

import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useLogin, useVersion } from '@/api/queries'
import { ApiError } from '@/api/client'

const loginSchema = z.object({
  username: z
    .string()
    .min(1, { message: 'Username is required' })
    .max(128, { message: 'Username too long' }),
  password: z.string().min(1, { message: 'Password is required' }),
  rememberMe: z.boolean(),
})

type LoginFormValues = z.infer<typeof loginSchema>

function describeError(error: ApiError): string {
  if (error.status === 401) {
    return 'Invalid username or password.'
  }
  if (error.status === 429) {
    if (error.retryAfterSeconds !== null) {
      return `Too many attempts. Try again in ${String(error.retryAfterSeconds)}s.`
    }
    return 'Too many attempts. Please wait a minute and try again.'
  }
  if (error.status >= 500) {
    return 'Server error. Please try again.'
  }
  return error.message
}

export function LoginPage() {
  const navigate = useNavigate()
  const versionQuery = useVersion()
  const login = useLogin()

  const form = useForm<LoginFormValues>({
    resolver: zodResolver(loginSchema),
    defaultValues: { username: '', password: '', rememberMe: false },
  })

  useEffect(() => {
    if (login.isSuccess) {
      void navigate({ to: '/overview' })
    }
  }, [login.isSuccess, navigate])

  if (versionQuery.isLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background p-6">
        <p className="text-muted-foreground">Loading…</p>
      </div>
    )
  }

  if (versionQuery.data?.users_configured === false) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background p-6">
        <Card className="w-full max-w-md">
          <CardHeader>
            <CardTitle>Welcome to homelab-monitor</CardTitle>
            <CardDescription>
              No users are configured yet. Create the first user with the CLI:
            </CardDescription>
          </CardHeader>
          <CardContent>
            <pre className="rounded-md bg-muted p-3 text-sm">
              <code>hm user create</code>
            </pre>
          </CardContent>
        </Card>
      </div>
    )
  }

  const onSubmit = form.handleSubmit((values) => {
    // TODO(STAGE-001-XXX): wire `rememberMe` once /api/auth/login accepts it.
    login.mutate({
      username: values.username,
      password: values.password,
    })
  })

  const errorMessage = login.error instanceof ApiError ? describeError(login.error) : null

  return (
    <div className="flex min-h-screen items-center justify-center bg-background p-6">
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle>Sign in</CardTitle>
          <CardDescription>Enter your homelab-monitor credentials.</CardDescription>
        </CardHeader>
        <form
          onSubmit={(e) => {
            void onSubmit(e)
          }}
          noValidate
        >
          <CardContent className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="username">Username</Label>
              <Input
                id="username"
                autoComplete="username"
                aria-invalid={form.formState.errors.username !== undefined}
                {...form.register('username')}
              />
              {form.formState.errors.username !== undefined && (
                <p role="alert" className="text-sm text-status-critical">
                  {form.formState.errors.username.message}
                </p>
              )}
            </div>
            <div className="space-y-2">
              <Label htmlFor="password">Password</Label>
              <Input
                id="password"
                type="password"
                autoComplete="current-password"
                aria-invalid={form.formState.errors.password !== undefined}
                {...form.register('password')}
              />
              {form.formState.errors.password !== undefined && (
                <p role="alert" className="text-sm text-status-critical">
                  {form.formState.errors.password.message}
                </p>
              )}
            </div>
            <label className="flex items-center gap-2 text-sm text-muted-foreground">
              <input type="checkbox" aria-label="Remember me" {...form.register('rememberMe')} />
              Remember me
            </label>
            {errorMessage !== null && (
              <p
                role="alert"
                className="rounded-md border border-status-critical bg-status-critical/10 p-2 text-sm text-status-critical"
              >
                {errorMessage}
              </p>
            )}
          </CardContent>
          <CardFooter>
            <Button type="submit" className="w-full" disabled={login.isPending}>
              {login.isPending ? 'Signing in…' : 'Sign in'}
            </Button>
          </CardFooter>
        </form>
      </Card>
    </div>
  )
}
