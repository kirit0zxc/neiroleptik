/**
 * Настройки камеры: как Юми стоит в кадре.
 *
 * Значения по умолчанию можно поменять в песочнице (вкладка «Камера») —
 * они сохраняются в camera.json рядом и попадают во все сборки.
 * Приложение всё равно может переопределить их опциями рендерера.
 *
 * Модуль без DOM и three: им же dev-сервер проверяет то, что сохраняет
 * песочница.
 */
export interface CameraSettings {
  /**
   * Что показываем:
   *  full     — вся фигура целиком
   *  half     — от бёдер вверх
   *  portrait — от груди вверх
   */
  fit: 'full' | 'half' | 'portrait';
  /** Воздух по краям кадра, доля от размера. 0.08 — восемь процентов. */
  margin: number;
  /** Крупность: больше единицы — ближе. */
  zoom: number;
  /** Сдвиг Юми в кадре по горизонтали, в метрах. Плюс — вправо. */
  offsetX: number;
  /** Сдвиг по вертикали, в метрах. Плюс — выше. */
  offsetY: number;
  /** Поворот камеры вокруг Юми, градусы. Плюс — заходим справа. */
  yaw: number;
  /** Наклон камеры, градусы. Плюс — смотрим сверху. */
  pitch: number;
  /** Угол обзора по вертикали, градусы. Меньше — плоский «телевик». */
  fov: number;
}

export const CAMERA_DEFAULTS: Readonly<CameraSettings> = {
  fit: 'full',
  margin: 0.08,
  zoom: 1,
  offsetX: 0,
  offsetY: 0,
  yaw: 0,
  pitch: 0,
  fov: 28,
};

/** Допустимые пределы — их же используют ползунки песочницы. */
export const CAMERA_LIMITS = {
  margin: [0, 0.5],
  zoom: [0.3, 4],
  offsetX: [-1, 1],
  offsetY: [-1, 1],
  yaw: [-180, 180],
  pitch: [-60, 60],
  fov: [10, 60],
} as const satisfies Record<Exclude<keyof CameraSettings, 'fit'>, readonly [number, number]>;

const FITS: readonly CameraSettings['fit'][] = ['full', 'half', 'portrait'];

/**
 * Взять из чего угодно только годные настройки, зажав их в пределы.
 * Неизвестное и битое отбрасывается — остаются значения по умолчанию.
 */
export function parseCamera(raw: unknown): Partial<CameraSettings> {
  const out: Partial<CameraSettings> = {};
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return out;
  const o = raw as Record<string, unknown>;

  if (FITS.includes(o.fit as CameraSettings['fit'])) out.fit = o.fit as CameraSettings['fit'];
  for (const key of Object.keys(CAMERA_LIMITS) as (keyof typeof CAMERA_LIMITS)[]) {
    const v = o[key];
    if (typeof v !== 'number' || !Number.isFinite(v)) continue;
    const [lo, hi] = CAMERA_LIMITS[key];
    out[key] = Math.round(Math.min(hi, Math.max(lo, v)) * 1000) / 1000;
  }
  return out;
}

/** Только то, что отличается от base, — для короткой записи в файл и код. */
export function cameraDiff(
  settings: CameraSettings, base: Readonly<CameraSettings> = CAMERA_DEFAULTS,
): Partial<CameraSettings> {
  const out: Partial<CameraSettings> = {};
  for (const key of Object.keys(settings) as (keyof CameraSettings)[]) {
    if (settings[key] !== base[key]) (out as Record<string, unknown>)[key] = settings[key];
  }
  return out;
}
