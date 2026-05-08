import { useMemo } from 'react'
import { cn } from '@/lib/utils'

interface SparklineProps {
  values: number[]
  width?: number
  height?: number
  ariaLabel: string
  className?: string
}

/**
 * Hand-rolled SVG sparkline for short numeric series. Renders nothing if
 * the series is empty. Y range auto-scales to [min, max] of the series.
 */
export function Sparkline({
  values,
  width = 160,
  height = 40,
  ariaLabel,
  className,
}: SparklineProps) {
  const path = useMemo(() => {
    if (values.length === 0) return ''
    const min = Math.min(...values)
    const max = Math.max(...values)
    const range = max - min === 0 ? 1 : max - min
    const stepX = values.length === 1 ? 0 : width / (values.length - 1)
    return values
      .map((v, i) => {
        const x = i * stepX
        const y = height - ((v - min) / range) * height
        return `${i === 0 ? 'M' : 'L'}${String(x.toFixed(2))},${String(y.toFixed(2))}`
      })
      .join(' ')
  }, [values, width, height])

  if (values.length === 0) {
    return null
  }

  return (
    <svg
      role="img"
      aria-label={ariaLabel}
      width={width}
      height={height}
      viewBox={`0 0 ${String(width)} ${String(height)}`}
      className={cn('text-primary', className)}
      data-values={values.join(',')}
    >
      <path
        d={path}
        fill="none"
        stroke="currentColor"
        strokeWidth={1.5}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}
