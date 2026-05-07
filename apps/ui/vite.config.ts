import { fileURLToPath, URL } from 'node:url'
import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig(({ mode }) => {
  // Read VITE_* env vars from .env, .env.local, .env.<mode>, .env.<mode>.local,
  // plus any vars set in the OS environment.
  const env = loadEnv(mode, process.cwd(), '')

  // Default API target matches the `backend-dev` Makefile (uvicorn on port 9090).
  const DEV_SERVER_PORT = Number(env['VITE_DEV_PORT'] ?? '5173')
  const DEV_SERVER_HOST = env['VITE_DEV_HOST'] ?? '0.0.0.0'
  const API_PROXY_TARGET = env['VITE_API_PROXY_TARGET'] ?? 'http://localhost:9090'

  return {
    plugins: [react(), tailwindcss()],
    server: {
      port: DEV_SERVER_PORT,
      host: DEV_SERVER_HOST,
      proxy: {
        '/api': {
          target: API_PROXY_TARGET,
          changeOrigin: true,
        },
      },
    },
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
  }
})
