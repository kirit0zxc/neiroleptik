// Расширение явно: модуль импортирует и vite.config.ts (nodenext).
import { parseCamera, type CameraSettings } from './camera.ts';

/**
 * Свой кадр для отдельных клипов: отличия от общего кадра.
 *
 * Ключ — имя клипа из реестра или путь к файлу, как в состоянии.
 * Хранится в clipCameras.json рядом и попадает в сборку; правится на
 * вкладке «Камера» песочницы в режиме «только для текущей анимации».
 *
 * Модуль без DOM: им же dev-сервер проверяет то, что сохраняет песочница.
 */
export type ClipCameras = Record<string, Partial<CameraSettings>>;

/** Разобрать файл: пустые и битые записи отбрасываются. */
export function parseClipCameras(raw: unknown): ClipCameras {
  const out: ClipCameras = Object.create(null);
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return out;
  for (const [clip, value] of Object.entries(raw)) {
    if (!clip || clip.length > 300 || clip === '__proto__') continue;
    const camera = parseCamera(value);
    if (Object.keys(camera).length) out[clip] = camera;
  }
  return out;
}
