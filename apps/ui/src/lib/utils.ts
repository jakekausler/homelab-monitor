import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

/**
 * Merge Tailwind class strings with conflict-resolution semantics.
 * Used by every shadcn-style component to combine variant classes
 * with caller-provided overrides.
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs))
}
