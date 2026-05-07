import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/queries', () => ({
  useVersion: vi.fn(),
  useLogin: vi.fn(),
}))

vi.mock('@tanstack/react-router', () => ({
  useNavigate: () => vi.fn(),
}))

import { useLogin, useVersion } from '@/api/queries'
import { ApiError } from '@/api/client'
import { LoginPage } from './Login'

const mockVersion = vi.mocked(useVersion)
const mockLogin = vi.mocked(useLogin)

function renderLogin() {
  const qc = new QueryClient()
  return render(
    <QueryClientProvider client={qc}>
      <LoginPage />
    </QueryClientProvider>,
  )
}

function setVersion(usersConfigured: boolean, isLoading = false) {
  mockVersion.mockReturnValue({
    data: isLoading
      ? undefined
      : {
          version: 'test',
          git_sha: 'abc',
          built_at: '2026-05-07T12:00:00Z',
          users_configured: usersConfigured,
        },
    isLoading,
  } as unknown as ReturnType<typeof useVersion>)
}

function setLogin(overrides: Partial<ReturnType<typeof useLogin>> = {}) {
  const base = {
    mutate: vi.fn(),
    isPending: false,
    isSuccess: false,
    error: null as ApiError | null,
  }
  mockLogin.mockReturnValue({
    ...base,
    ...overrides,
  } as unknown as ReturnType<typeof useLogin>)
}

afterEach(() => {
  cleanup()
})

beforeEach(() => {
  vi.clearAllMocks()
})

describe('LoginPage', () => {
  it('shows the welcome message when no users are configured', () => {
    setVersion(false)
    setLogin()
    renderLogin()
    expect(screen.getByText(/Welcome to homelab-monitor/)).toBeInTheDocument()
    expect(screen.getByText(/hm user create/)).toBeInTheDocument()
  })

  it('renders the form when users are configured', () => {
    setVersion(true)
    setLogin()
    renderLogin()
    expect(screen.getByLabelText('Username')).toBeInTheDocument()
    expect(screen.getByLabelText('Password')).toBeInTheDocument()
  })

  it('flags blank username and password on submit', async () => {
    setVersion(true)
    setLogin()
    renderLogin()
    await userEvent.click(screen.getByRole('button', { name: /Sign in/ }))
    expect(await screen.findByText('Username is required')).toBeInTheDocument()
    expect(screen.getByText('Password is required')).toBeInTheDocument()
  })

  it('calls login.mutate with the form values', async () => {
    setVersion(true)
    const mutate = vi.fn()
    setLogin({ mutate })
    renderLogin()
    await userEvent.type(screen.getByLabelText('Username'), 'alice')
    await userEvent.type(screen.getByLabelText('Password'), 'hunter2')
    await userEvent.click(screen.getByRole('button', { name: /Sign in/ }))
    await waitFor(() => {
      expect(mutate).toHaveBeenCalledWith({
        username: 'alice',
        password: 'hunter2',
      })
    })
  })

  it('renders the 401 error message', () => {
    setVersion(true)
    setLogin({
      error: new ApiError({
        status: 401,
        code: 'wrong_password',
        message: 'invalid username or password',
        retryAfterSeconds: null,
        details: null,
      }),
    })
    renderLogin()
    expect(screen.getByText('Invalid username or password.')).toBeInTheDocument()
  })

  it('renders the 429 message with retry-after countdown', () => {
    setVersion(true)
    setLogin({
      error: new ApiError({
        status: 429,
        code: 'rate_limited',
        message: 'too many',
        retryAfterSeconds: 30,
        details: null,
      }),
    })
    renderLogin()
    expect(screen.getByText('Too many attempts. Try again in 30s.')).toBeInTheDocument()
  })

  it('renders the generic 500 message', () => {
    setVersion(true)
    setLogin({
      error: new ApiError({
        status: 500,
        code: 'internal_error',
        message: 'oops',
        retryAfterSeconds: null,
        details: null,
      }),
    })
    renderLogin()
    expect(screen.getByText('Server error. Please try again.')).toBeInTheDocument()
  })

  // Line 65: isLoading branch
  it('shows "Loading…" while version is loading', () => {
    setVersion(false, /* isLoading */ true)
    setLogin()
    renderLogin()
    expect(screen.getByText('Loading…')).toBeInTheDocument()
    expect(screen.queryByLabelText('Username')).not.toBeInTheDocument()
  })

  // Line 159: disabled button while isPending
  it('disables the Sign in button and shows "Signing in…" while pending', () => {
    setVersion(true)
    setLogin({ isPending: true })
    renderLogin()
    const btn = screen.getByRole('button', { name: /Signing in/ })
    expect(btn).toBeDisabled()
  })

  // Line 40: 429 with retryAfterSeconds === null
  it('renders the 429 message without countdown when retryAfterSeconds is null', () => {
    setVersion(true)
    setLogin({
      error: new ApiError({
        status: 429,
        code: 'rate_limited',
        message: 'too many',
        retryAfterSeconds: null,
        details: null,
      }),
    })
    renderLogin()
    expect(
      screen.getByText('Too many attempts. Please wait a minute and try again.'),
    ).toBeInTheDocument()
  })

  // Line 45: fallback to error.message for non-401/429/5xx
  it('shows the raw error message for unexpected status codes', () => {
    setVersion(true)
    setLogin({
      error: new ApiError({
        status: 403,
        code: 'forbidden',
        message: 'account disabled',
        retryAfterSeconds: null,
        details: null,
      }),
    })
    renderLogin()
    expect(screen.getByText('account disabled')).toBeInTheDocument()
  })
})
