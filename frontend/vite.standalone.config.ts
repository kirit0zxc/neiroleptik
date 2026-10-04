import { defineConfig } from 'vite'

/**
 * Сборка одним файлом для content script расширения.
 *
 * Здесь всё внутри, включая three: у content script нет модульной
 * системы и нет возможности что-то доставить рядом. React сюда
 * намеренно не входит — точка входа standalone.ts обходится без него.
 */
export default defineConfig({
  // Модель и клипы раздаются отдельно — в бандл их не копируем.
  publicDir: false,
  build: {
    outDir: 'dist-standalone',
    emptyOutDir: true,
    lib: {
      entry: 'src/mascot/standalone.ts',
      formats: ['iife'],
      name: 'Mascot',
      fileName: () => 'mascot.global.js',
    },
  },
})
