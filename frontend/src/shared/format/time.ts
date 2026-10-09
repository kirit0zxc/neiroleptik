/**
 * Время: API отдаёт UTC (ISO 8601), на экране — локальное с поясом (F13).
 */
const FULL = new Intl.DateTimeFormat('ru-RU', {
  year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', timeZoneName: 'short',
});

export function formatTime(iso: string, formatter: Intl.DateTimeFormat = FULL): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? 'неизвестно' : formatter.format(d);
}

/** «5 с назад», «3 мин назад» — для last_seen. */
export function ago(iso: string, now: number = Date.now()): string {
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return 'неизвестно';
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 60) return `${s} с назад`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} мин назад`;
  const h = Math.round(m / 60);
  if (h < 48) return `${h} ч назад`;
  return `${Math.round(h / 24)} дн назад`;
}
