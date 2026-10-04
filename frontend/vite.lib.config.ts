import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

/**
 * Сборка пакета для обычного фронта.
 *
 * Три входа, чтобы приложение тянуло только нужное:
 *   mascot        — ядро, мост, голос; без three и React
 *   mascot/vrm    — 3D-рендерер
 *   mascot/react  — компонент и хук; рендерер подгружается лениво
 *
 * React и three остаются внешними: в приложении они уже есть,
 * и второй экземпляр сломал бы и хуки, и сцену.
 */
export default defineConfig({
  plugins: [react()],
  publicDir: false,
  build: {
    outDir: 'dist-lib',
    emptyOutDir: true,
    sourcemap: true,
    lib: {
      entry: {
        index: 'src/mascot/index.ts',
        vrm: 'src/mascot/vrm.ts',
        react: 'src/mascot/react.ts',
      },
      formats: ['es'],
    },
    rollupOptions: {
      external: [
        'react', 'react-dom', 'react/jsx-runtime',
        /^three($|\/)/, /^@pixiv\//,
      ],
    },
  },
})
