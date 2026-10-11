import { ApiError, errorFrom, notifyUnauthorized } from '../../shared/api/client';

const DAY = 86_400_000;
/** Предел периода выгрузки IoC (specs/investigation-handoff.md). */
export const IOC_MAX_DAYS = 31;

/** Значение для `<input type="datetime-local">` в локальном времени (F13). */
export function toLocalInput(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/**
 * Период выгрузки IoC: обе даты обязательны, `from < to`, не больше 31 дня,
 * полуинтервал [from, to). Сервер умолчаний не имеет — «последние 24 часа»
 * выбирает интерфейс и всегда отправляет обе даты.
 */
export class IocPeriod {
  readonly from: Date;
  readonly to: Date;

  constructor(from: Date, to: Date) {
    this.from = from;
    this.to = to;
  }

  static lastDay(now = new Date()) {
    // Минуты без секунд: так же, как в поле ввода.
    const to = new Date(now);
    to.setSeconds(0, 0);
    return new IocPeriod(new Date(to.getTime() - DAY), to);
  }

  /** Из значений `datetime-local` (локальное время); пустое — Invalid Date. */
  static fromInputs(from: string, to: string) {
    return new IocPeriod(new Date(from), new Date(to));
  }

  /** Текст ошибки или null — проверка до запроса. */
  validate(): string | null {
    const f = this.from.getTime();
    const t = this.to.getTime();
    if (Number.isNaN(f) || Number.isNaN(t)) return 'Укажите обе даты.';
    if (!(f < t)) return 'Начало периода должно быть раньше конца.';
    if (t - f > IOC_MAX_DAYS * DAY) return `Период — не больше ${IOC_MAX_DAYS} дня.`;
    return null;
  }

  /** Параметры запроса: всегда обе даты, RFC 3339 UTC. */
  toQuery(deploymentId?: string) {
    const q = new URLSearchParams({ from: this.from.toISOString(), to: this.to.toISOString() });
    if (deploymentId) q.set('deployment_id', deploymentId);
    return q;
  }
}

/**
 * Имя файла из `Content-Disposition`: `filename*=UTF-8''…` важнее `filename`.
 * Путь и управляющие символы отрезаются — имя только для сохранения файла.
 */
export function filenameFrom(header: string | null, fallback: string): string {
  if (!header) return fallback;
  const star = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(header);
  let name: string | null = null;
  if (star) {
    try { name = decodeURIComponent(star[1]!.trim()); } catch { name = null; }
  }
  name ??= /filename\s*=\s*"([^"]*)"/i.exec(header)?.[1] ?? /filename\s*=\s*([^;]+)/i.exec(header)?.[1]?.trim() ?? null;
  // eslint-disable-next-line no-control-regex
  const safe = name?.split(/[\\/]/).pop()?.replace(/[\u0000-\u001f\u007f]/g, '').trim();
  return safe || fallback;
}

/** Понятный текст ошибки скачивания артефакта по коду центра. */
export function artifactErrorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.code === 'ARTIFACT_PENDING') return 'Файл ещё не готов — проверьте позже.';
    if (err.code === 'ARTIFACT_FAILED') return 'Файл не удалось получить.';
    if (err.code === 'ARTIFACT_EXPIRED' || err.status === 410) return 'Файл удалён по сроку хранения.';
    if (err.status === 403) return 'Скачивать артефакты могут только оператор и администратор.';
  }
  return err instanceof Error && err.message ? err.message : 'Не удалось скачать файл.';
}

/**
 * Скачать файл как вложение: без предпросмотра и без открытия в браузере (S5).
 * Ошибки — ApiError, как у остальных запросов.
 */
export async function downloadFile(path: string, fallbackName: string): Promise<string> {
  let res: Response;
  try {
    res = await fetch(path, { credentials: 'include' });
  } catch {
    throw new ApiError(0, 'Нет связи с сервером');
  }
  if (res.status === 401) notifyUnauthorized();
  if (!res.ok) throw await errorFrom(res);
  const name = filenameFrom(res.headers.get('Content-Disposition'), fallbackName);
  // Тип всегда octet-stream: браузер сохраняет, а не показывает содержимое.
  const blob = new Blob([await res.arrayBuffer()], { type: 'application/octet-stream' });
  const url = URL.createObjectURL(blob);
  try {
    const a = document.createElement('a');
    a.href = url;
    a.download = name;
    a.rel = 'noopener';
    document.body.append(a);
    a.click();
    a.remove();
  } finally {
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }
  return name;
}

/** Размер файла для людей. */
export function formatBytes(n: number | null): string {
  if (n === null) return '—';
  if (n < 1024) return `${n} Б`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1).replace('.', ',')} КБ`;
  return `${(n / 1024 / 1024).toFixed(1).replace('.', ',')} МБ`;
}
