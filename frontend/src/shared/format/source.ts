/**
 * Источник и сервис события с защитой от неполных данных (specs/frontend.md,
 * «Неполные данные телеметрии»). По схеме EventV1 `source` обязателен, но
 * данные приходят от ловушек — экран не должен падать, если поля нет.
 */

export const UNKNOWN_SOURCE = 'источник неизвестен';

const record = (v: unknown): Record<string, unknown> | null =>
  (v && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, unknown> : null);

/** IP источника или null, если его нет или он не строка. */
export function sourceIp(e: { source?: unknown }): string | null {
  const ip = record(e.source)?.ip;
  return typeof ip === 'string' && ip ? ip : null;
}

/** «ip:port», «ip» без порта или «источник неизвестен». */
export function sourceLabel(e: { source?: unknown }): string {
  const ip = sourceIp(e);
  if (!ip) return UNKNOWN_SOURCE;
  const port = record(e.source)?.port;
  return typeof port === 'number' && Number.isInteger(port) ? `${ip}:${port}` : ip;
}

/** «tcp :2222» по service события; пусто, если сервиса нет. */
export function serviceLabel(service: unknown): string {
  const s = record(service);
  const protocol = typeof s?.protocol === 'string' ? s.protocol : '';
  const port = typeof s?.port === 'number' && Number.isInteger(s.port) ? `:${s.port}` : '';
  return [protocol, port].filter(Boolean).join(' ');
}
