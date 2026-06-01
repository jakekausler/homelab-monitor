import { mergeConfig } from 'vite'
import { defineConfig } from 'vitest/config'
import viteConfig from './vite.config'

// vite.config.ts is a function (uses loadEnv) — resolve it for the vitest
// context. `command: 'serve'` is correct since vitest does not run a build.
const resolvedViteConfig = viteConfig({ mode: 'test', command: 'serve' })

export default mergeConfig(
  resolvedViteConfig,
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
          'src/routes/__root.tsx',
          'src/router.tsx',
          'src/api/crons.ts',
          // CodeMirror 6 EditorView is non-functional in jsdom; LogsQlEditorImpl
          // is validated via the textarea-fallback unit tests + manual Refinement.
          'src/components/logs/LogsQlEditorImpl.tsx',
          'playwright/**',
          '**/*.test.{ts,tsx}',
        ],
      },
    },
  }),
)
