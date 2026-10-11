import type { HfEvent } from '../../shared/api/types';
import { maskSecret, sanitizeTerminal } from '../../shared/security/terminal';

const TYPE_LABEL: Record<string, string> = {
  'connection.opened': 'Подключение',
  'auth.attempt': 'Попытка входа',
  'session.command': 'Команда',
  'session.output': 'Вывод',
  'http.request': 'HTTP-запрос',
  'artifact.observed': 'Файл',
  'token.triggered': 'Honeytoken',
  'session.closed': 'Сессия закрыта',
  'runtime.state': 'Состояние ловушки',
};

export function eventLabel(type: string) { return TYPE_LABEL[type] ?? `Неизвестное событие (${type})`; }

const str = (v: unknown) => (typeof v === 'string' ? v : v == null ? '' : JSON.stringify(v));

/**
 * Пароль попытки входа (S4). Маскирует сервер: вместо `data.password` —
 * boolean `data.password_present` (true — «пароль скрыт», false — «пароль
 * не записан»; нет флага — неизвестно, null, показываем только логин).
 * Пришедший `data.password` — ошибка сервера: не показываем (без флага —
 * своя маска) и предупреждаем в консоли.
 */
let passwordWarned = false;

export function passwordView(d: Record<string, unknown>): string | null {
  // Один раз за сессию: демо-фикстуры содержат исходные пароли — не засоряем консоль.
  if (d.password !== undefined && !passwordWarned) {
    passwordWarned = true;
    console.warn('HoneyForge: сервер передал data.password — пароль должен маскироваться на сервере (S4)');
  }
  if (d.password_present === true) return 'пароль скрыт';
  if (d.password_present === false) return 'пароль не записан';
  if (typeof d.password === 'string') return maskSecret(d.password);
  return null;
}

/**
 * Краткое описание события — только текст (S1). Данные атакующего
 * очищаются от управляющих символов (S2), пароли маскируются (S4).
 * Результат выводится как текстовый узел, без разметки.
 */
export function describeEvent(e: Pick<HfEvent, 'type' | 'data'>): string {
  const d = e.data;
  const trunc = d.truncated === true ? ' [обрезано]' : '';
  switch (e.type) {
    case 'auth.attempt': {
      const ok = d.success === true ? 'успешно' : d.success === false ? 'отказ' : '';
      const pass = passwordView(d);
      return sanitizeTerminal([str(d.username), pass === null ? null : `/ ${pass}`, ok].filter(Boolean).join(' '));
    }
    case 'session.command':
    case 'session.output':
      return sanitizeTerminal(str(d.input ?? d.output)) + trunc;
    case 'http.request':
      return sanitizeTerminal(`${str(d.method)} ${str(d.path)}`) + trunc;
    case 'token.triggered':
      // URL приманки не делаем ссылкой и не открываем (S3).
      return sanitizeTerminal(`приманка ${str(d.token_id)}`);
    default:
      return sanitizeTerminal(Object.keys(d).length ? JSON.stringify(d) : '');
  }
}
