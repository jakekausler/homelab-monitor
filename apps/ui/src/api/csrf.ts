const CSRF_COOKIE_NAME = 'homelab_monitor_csrf'

/**
 * Read the CSRF token from `document.cookie`.
 *
 * The backend sets `homelab_monitor_csrf` with `httponly=False` so the JS
 * client can echo it back as the `X-CSRF-Token` header on state-changing
 * requests. Returns `null` if the cookie is absent or empty.
 */
export function getCsrfToken(): string | null {
  if (typeof document === 'undefined') {
    return null
  }
  const cookies = document.cookie.split(';')
  for (const raw of cookies) {
    const [name, ...rest] = raw.trim().split('=')
    if (name === CSRF_COOKIE_NAME) {
      const value = rest.join('=')
      return value.length > 0 ? decodeURIComponent(value) : null
    }
  }
  return null
}

export const CSRF_HEADER = 'X-CSRF-Token'
