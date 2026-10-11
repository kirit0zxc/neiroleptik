import type { HfEvent } from '../../shared/api/types';
import { sanitizeTerminal } from '../../shared/security/terminal';

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
 * Пришедший `data.password` — ошибка сервера: значение не показываем вовсе
 * («пароль скрыт») и предупреждаем в консоли.
 */
let passwordWarned = false;
/** Один раз за сессию: демо-фикстуры содержат исходные пароли — не засоряем консоль. Значение не пишем. */
function warnSecret() {
  if (passwordWarned) return;
  passwordWarned = true;
  console.warn('HoneyForge: сервер передал пароль или секрет в data — он должен маскироваться на сервере (S4)');
}
/** Только для тестов: снова разрешить предупреждение. */
export function resetSecretWarning() { passwordWarned = false; }
/** Ключи данных, которые не выводятся никогда (S4). */
const SECRET_KEY = /pass(word|wd)?|secret|credential/i;

export function passwordView(d: Record<string, unknown>): string | null {
  if (d.password !== undefined) warnSecret();
  if (d.password_present === true) return 'пароль скрыт';
  if (d.password_present === false) return 'пароль не записан';
  // Пришедший пароль — тоже «скрыт»: ни длины, ни крайних символов (как у сервера).
  if (d.password !== undefined) return 'пароль скрыт';
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
      // Исход входа — через «—», чтобы не читался как часть фразы о пароле.
      const who = [str(d.username) || '(без логина)', pass === null ? null : `/ ${pass}`].filter(Boolean).join(' ');
      return sanitizeTerminal(ok ? `${who} — ${ok}` : who);
    }
    case 'session.command':
    case 'session.output':
      return sanitizeTerminal(str(d.input ?? d.output)) + trunc;
    case 'http.request':
      return sanitizeTerminal(`${str(d.method)} ${str(d.path)}`) + trunc;
    case 'token.triggered':
      // URL приманки не делаем ссылкой и не открываем (S3).
      return sanitizeTerminal(`приманка ${str(d.token_id)}`);
    default: {
      // Неизвестный тип — данные текстом, но без секретов: пароль в нём — ошибка сервера (S4).
      const entries = Object.entries(d);
      if (entries.some(([k]) => SECRET_KEY.test(k))) warnSecret();
      const safe = Object.fromEntries(entries.filter(([k]) => !SECRET_KEY.test(k)));
      return sanitizeTerminal(Object.keys(safe).length ? JSON.stringify(safe) : '');
    }
  }
}
