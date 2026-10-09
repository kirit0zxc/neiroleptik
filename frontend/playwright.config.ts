import { defineConfig } from '@playwright/test';

// E2E по умолчанию идут на моках (MSW), чтобы не зависеть от стенда.
// На реальном API: E2E_BASE_URL=https://… npm run e2e
const external = process.env.E2E_BASE_URL;

export default defineConfig({
  testDir: 'e2e',
  // В CI — ещё HTML-отчёт: его выкладывает workflow при падении.
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: external ?? 'http://127.0.0.1:5174',
    // Локально — установленный Edge, без скачивания браузеров; в CI — PW_CHANNEL=chromium.
    channel: process.env.PW_CHANNEL ?? 'msedge',
  },
  webServer: external ? undefined : {
    command: 'npx vite --port 5174 --strictPort',
    env: { VITE_API_MOCK: '1' },
    url: 'http://127.0.0.1:5174',
    reuseExistingServer: !process.env.CI,
  },
});
