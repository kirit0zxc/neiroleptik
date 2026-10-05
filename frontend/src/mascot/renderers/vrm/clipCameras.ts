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

const FILE_KEY = /^file:(.*):(?:none|inplace|legs|strict)$/;

/**
 * Путь в той кодировке, в какой его пишут реестр, студия и цепочки:
 * каждый сегмент — encodeURIComponent. Уже закодированный сегмент
 * сначала раскодируется, поэтому функция идемпотентна.
 */
function canonicalPath(path: string): string {
  return path.split('/').map((part) => {
    let plain = part;
    try { plain = decodeURIComponent(part); } catch { /* битая кодировка — кодируем как есть */ }
    // NFC: имена из архивов macOS приходят разложенными (NFD), а реестр
    // и студия пишут их в составной форме.
    return encodeURIComponent(plain.normalize('NFC'));
  }).join('/');
}

/**
 * Под каким именем хранить кадр клипа. Обычно это имя клипа как есть.
 * Файл, запущенный из списка песочницы, играет под служебным ключом
 * `file:<url>:<маска>`, а состояния ссылаются на тот же файл путём от
 * assetsBase в кодировке реестра (`anim/vrma/Happy%20Idle.vrma`). Ключ
 * приводится к этому пути без маски, иначе сохранённый в песочнице кадр
 * не совпал бы ни с одним состоянием. Если путь есть в реестре клипов,
 * рендерер дополнительно заменяет его логическим именем.
 */
export function clipCameraKey(name: string, assetsBase: string): string {
  const m = FILE_KEY.exec(name);
  if (!m) return name;
  const url = m[1];
  // Адрес со схемой (https:, blob:) — не путь от assetsBase, оставляем.
  if (/^[a-z][a-z\d+.-]*:/i.test(url)) return url;
  const path = canonicalPath(url);
  const base = canonicalPath(assetsBase.endsWith('/') ? assetsBase : assetsBase + '/');
  return path.startsWith(base) ? path.slice(base.length) : path;
}
