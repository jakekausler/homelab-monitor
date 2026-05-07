import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

const DEV_SERVER_PORT = Number(process.env['VITE_DEV_PORT'] ?? '5173')
const DEV_SERVER_HOST = process.env['VITE_DEV_HOST'] ?? '0.0.0.0'
const API_PROXY_TARGET = process.env['VITE_API_PROXY_TARGET'] ?? 'http://localhost:9090'

export default defineConfig({
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
})
