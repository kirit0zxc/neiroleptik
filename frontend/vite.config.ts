import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

// Адрес API для разработки; без него работает MSW (VITE_API_MOCK=1).
const API = process.env.VITE_API_PROXY ?? 'http://127.0.0.1:8000';

export default defineConfig({
  plugins: [react()],
  server: {
    // 127.0.0.1, а не localhost: на Windows Vite иначе слушает только IPv6.
    host: '127.0.0.1',
    port: 5173,
    proxy: { '/api': { target: API, changeOrigin: true, ws: true } },
  },
  test: {
    environment: 'happy-dom',
    include: ['src/**/*.test.{ts,tsx}'],
    setupFiles: ['src/test/setup.ts'],
  },
});
