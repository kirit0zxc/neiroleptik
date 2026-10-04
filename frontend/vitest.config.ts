import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    // Ядру нужны window, performance и requestAnimationFrame.
    environment: 'happy-dom',
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
