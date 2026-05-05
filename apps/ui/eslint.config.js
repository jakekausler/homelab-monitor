// @ts-check
import js from '@eslint/js'
import tseslint from 'typescript-eslint'
import reactPlugin from '@eslint-react/eslint-plugin'
import reactHooksPlugin from 'eslint-plugin-react-hooks'

export default tseslint.config(
  // Global ignores
  {
    ignores: ['dist/', 'node_modules/', 'coverage/', 'playwright-report/', 'test-results/'],
  },
  // Base JS recommended
  js.configs.recommended,
  // Type-aware rules — src files ONLY
  {
    files: ['src/**/*.{ts,tsx}'],
    extends: [
      ...tseslint.configs.recommendedTypeChecked,
      reactPlugin.configs['recommended-type-checked'],
    ],
    languageOptions: {
      parserOptions: {
        project: './tsconfig.app.json',
        tsconfigRootDir: import.meta.dirname,
      },
    },
    plugins: {
      'react-hooks': reactHooksPlugin,
    },
    rules: {
      ...(reactHooksPlugin.configs['recommended-latest']?.rules ??
        reactHooksPlugin.configs.recommended.rules),
      '@typescript-eslint/no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
      ],
    },
  },
  // Non-type-aware rules for config files (no parserOptions.project)
  {
    files: ['*.config.{js,ts,cjs,mjs}'],
    extends: [...tseslint.configs.recommended],
    rules: {},
  },
)
