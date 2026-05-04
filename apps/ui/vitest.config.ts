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
          lines: 100,
          branches: 100,
          functions: 100,
          statements: 100,
        },
        exclude: [
          '**/node_modules/**',
          '**/dist/**',
          '**/*.config.{ts,js}',
          '**/*.d.ts',
          'src/main.tsx',
          'src/test/**',
          'playwright/**',
          '**/*.test.{ts,tsx}',
        ],
      },
    },
  }),
)
