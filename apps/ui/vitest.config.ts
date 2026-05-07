import { mergeConfig } from 'vite'
import { defineConfig } from 'vitest/config'
import viteConfig from './vite.config'

export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      environment: 'jsdom',
      setupFiles: ['./src/test/setup.ts'],
      globals: false,
      coverage: {
        provider: 'v8',
        reporter: ['text', 'html'],
        thresholds: {
          lines: 80,
          branches: 75,
          functions: 80,
          statements: 80,
        },
        exclude: [
          '**/node_modules/**',
          '**/dist/**',
          '**/*.config.{ts,js}',
          '**/*.d.ts',
          'src/main.tsx',
          'src/test/**',
          'src/api/schema.ts',
          'src/components/ui/**',
          'src/router.tsx',
          'src/routes/__root.tsx',
          'playwright/**',
          '**/*.test.{ts,tsx}',
        ],
      },
    },
  }),
)
