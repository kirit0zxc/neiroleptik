import { resolveAsset } from '../../assets';

/** Картинка фона по умолчанию — относительно assetsBase. */
export const DEFAULT_BACKDROP = 'bg/room.webp';

/**
 * Адрес картинки фона из пропа backdrop: true — картинка по умолчанию из
 * папки ассетов, строка — свой адрес (относительный — тоже от assetsBase,
 * как модель и клипы), false — без фона.
 */
export function backdropUrl(backdrop: boolean | string, assetsBase = '/mascot/'): string | null {
  if (backdrop === true) return resolveAsset(DEFAULT_BACKDROP, assetsBase);
  // Не строка (false, а из JS — и null) или одни пробелы — без фона.
  const src = typeof backdrop === 'string' ? backdrop.trim() : '';
  return src ? resolveAsset(src, assetsBase) : null;
}

/**
 * Значение для background-image. Кавычки, обратный слэш и переводы строк
 * экранируются: иначе адрес с ними сделал бы правило недействительным,
 * и фон молча пропал бы.
 */
export function cssUrl(url: string): string {
  const escaped = url
    .replace(/["\\]/g, (c) => '\\' + c)
    .replace(/[\n\r\f]/g, (c) => '\\' + c.charCodeAt(0).toString(16) + ' ');
  return 'url("' + escaped + '")';
}
