/**
 * Возможности серверной телеметрии, включаемые по мере готовности backend.
 * Выделены в отдельный модуль, чтобы ими могли пользоваться и маршрутизация
 * (App), и экраны (например, «Обзор» гейтит панель последних событий), без
 * циклических импортов.
 *
 *   - VITE_EVENTS_ENABLED    — лента событий (`/events`) и живой поток (`/stream`);
 *   - VITE_ANALYTICS_ENABLED — сводка обзора (`/analytics/summary`);
 *   - VITE_REPLAY_ENABLED    — запись сессии (replay).
 *
 * VITE_TELEMETRY_ENABLED — общий выключатель (все три сразу), моки
 * (VITE_API_MOCK=1) включают всё. Доступ к import.meta.env — только по явным
 * именам, чтобы Vite статически подставил значения при сборке.
 */
const flagOn = (v: string | undefined) => v === '1';
const onMocks = () => flagOn(import.meta.env.VITE_API_MOCK);
const telemetryAll = () => flagOn(import.meta.env.VITE_TELEMETRY_ENABLED);

/** Лента событий и поток готовы на сервере (или моки/umbrella). */
export function eventsEnabled() {
  return onMocks() || telemetryAll() || flagOn(import.meta.env.VITE_EVENTS_ENABLED);
}
/** Сводка обзора готова на сервере (или моки/umbrella). */
export function analyticsEnabled() {
  return onMocks() || telemetryAll() || flagOn(import.meta.env.VITE_ANALYTICS_ENABLED);
}
/** Запись сессии (replay) готова на сервере (или моки/umbrella). */
export function replayEnabled() {
  return onMocks() || telemetryAll() || flagOn(import.meta.env.VITE_REPLAY_ENABLED);
}
