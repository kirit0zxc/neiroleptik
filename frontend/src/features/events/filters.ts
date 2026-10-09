import type { HfEvent } from '../../shared/api/types';

/**
 * Фильтры ленты (specs/frontend.md, F11 и «Лента: фильтры и живой поток»).
 * Хранятся в адресе страницы; `from`/`to` — локальное время из поля
 * `datetime-local`, в API уходят в UTC.
 */
export interface EventFilters {
  deployment_id?: string;
  type?: string;
  source_ip?: string;
  /** Локальное время `YYYY-MM-DDTHH:mm`. */
  from?: string;
  to?: string;
}

const KEYS = ['deployment_id', 'type', 'source_ip', 'from', 'to'] as const;

export function filtersFromSearch(params: URLSearchParams): EventFilters {
  const f: EventFilters = {};
  for (const k of KEYS) {
    const v = params.get(k)?.trim();
    if (v) f[k] = v;
  }
  return f;
}

export function filtersToSearch(f: EventFilters): URLSearchParams {
  const p = new URLSearchParams();
  for (const k of KEYS) if (f[k]) p.set(k, f[k]!);
  return p;
}

const IPV4 = /^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/;

/** IPv4 или IPv6 (проверка через разбор URL — без своих регулярок для v6). */
export function isIp(value: string): boolean {
  if (IPV4.test(value)) return true;
  if (!value.includes(':')) return false;
  try { return new URL(`http://[${value}]/`).hostname.length > 2; } catch { return false; }
}

/** Ошибки фильтров для показа у полей; пустой объект — всё верно. */
export function validateFilters(f: EventFilters): Partial<Record<keyof EventFilters, string>> {
  const errors: Partial<Record<keyof EventFilters, string>> = {};
  if (f.source_ip && !isIp(f.source_ip)) errors.source_ip = 'Нужен IPv4 или IPv6';
  const from = f.from ? localToUtc(f.from) : null;
  const to = f.to ? localToUtc(f.to) : null;
  if (f.from && !from) errors.from = 'Неверная дата';
  if (f.to && !to) errors.to = 'Неверная дата';
  // to — исключительно: при from = to выборка пустая, это тоже ошибка.
  if (from && to && from >= to) errors.to = '«До» должно быть позже «с»';
  return errors;
}

/** Локальное `YYYY-MM-DDTHH:mm` → ISO UTC; null — если дата битая. */
export function localToUtc(local: string): string | null {
  const d = new Date(local);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

/** Параметры запроса `GET /events` (время — уже UTC). */
export function toQuery(f: EventFilters, cursor?: string | null, limit = 50): URLSearchParams {
  const q = new URLSearchParams();
  if (f.deployment_id) q.set('deployment_id', f.deployment_id);
  if (f.type) q.set('type', f.type);
  if (f.source_ip) q.set('source_ip', f.source_ip);
  const from = f.from && localToUtc(f.from);
  const to = f.to && localToUtc(f.to);
  if (from) q.set('from', from);
  if (to) q.set('to', to);
  if (cursor) q.set('cursor', cursor);
  q.set('limit', String(limit));
  return q;
}

/** Подходит ли живое событие под фильтры — как у сервера: from включительно, to исключительно. */
export function matches(e: HfEvent, f: EventFilters): boolean {
  if (f.deployment_id && e.deployment_id !== f.deployment_id) return false;
  if (f.type && e.type !== f.type) return false;
  if (f.source_ip && e.source.ip !== f.source_ip) return false;
  const at = new Date(e.occurred_at).getTime();
  const from = f.from ? new Date(f.from).getTime() : null;
  const to = f.to ? new Date(f.to).getTime() : null;
  if (from !== null && !(at >= from)) return false;
  if (to !== null && !(at < to)) return false;
  return true;
}
