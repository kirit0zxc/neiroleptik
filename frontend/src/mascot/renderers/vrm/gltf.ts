import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { MeshoptDecoder } from 'three/examples/jsm/libs/meshopt_decoder.module.js';

/**
 * GLTFLoader, который понимает сжатые ассеты.
 *
 * Модель и клипы проходят через scripts/optimize-assets.mjs: геометрия
 * и анимации сжимаются meshopt без потерь. Декодер весит ~10 КБ в gzip
 * и работает на WebAssembly — несжатые файлы он не замедляет.
 */
export function createGLTFLoader() {
  return new GLTFLoader().setMeshoptDecoder(MeshoptDecoder);
}
