import { defineConfig } from '@playwright/test';

// E2E по умолчанию идут на моках (MSW), чтобы не зависеть от стенда.
// На реальном API: E2E_BASE_URL=https://… npm run e2e
const external = process.env.E2E_BASE_URL;

export default defineConfig({
  testDir: 'e2e',
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: process.env.CI ? 2 : undefined,
  // В CI — ещё HTML-отчёт: его выкладывает workflow при падении.
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: external ?? 'http://127.0.0.1:5174',
    // Локально — установленный Edge, без скачивания браузеров; в CI — PW_CHANNEL=chromium.
    channel: process.env.PW_CHANNEL ?? 'msedge',
    // Функциональные сценарии идут со штатным портретом Юми: программный
    // WebGL на CI задерживает клики и polling. 3D требует отдельного GPU-стенда.
    launchOptions: { args: ['--disable-webgl'] },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: external ? undefined : {
    // Собранный интерфейс без HMR и преобразований dev-сервера: под нагрузкой
    // в dev-режиме воспроизводились обрывы тела ответов MSW при опросе заданий.
    command: 'node node_modules/vite/bin/vite.js build && node node_modules/vite/bin/vite.js preview --host 127.0.0.1 --port 5174 --strictPort',
    env: { VITE_API_MOCK: '1' },
    url: 'http://127.0.0.1:5174',
    // Чужой процесс на этом порту может обслуживать другой код или живой API.
    reuseExistingServer: false,
  },
});
