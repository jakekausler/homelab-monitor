import type { components } from '@/api/schema'

/**
 * Helper to extract a schema component as a non-nullable type.
 *
 * Under tsconfig's `noUncheckedIndexedAccess: true`, `components['schemas']['X']`
 * widens to `X | undefined`. This helper strips the `undefined` so consumers get
 * the actual type. Required because openapi-typescript generates indexed-access
 * type aliases.
 */
export type Schema<K extends keyof components['schemas']> = NonNullable<components['schemas'][K]>
