import type { HfEvent } from '../../shared/api/types';
import { eventData, serviceLabel } from '../../shared/format/source';
import { sanitizeTerminal } from '../../shared/security/terminal';

const TYPE_LABEL: Record<string, string> = {
  'connection.opened': 'Подключение',
  'auth.attempt': 'Попытка входа',
  'session.command': 'Команда',
  'session.output': 'Вывод',
  'http.request': 'HTTP-запрос',
  'tcp.request': 'TCP-запрос',
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
/** Слова ключей, которые не выводятся никогда (S4); сравниваются целые слова ключа. */
const SECRET_WORDS = new Set(['pass', 'password', 'passwd', 'passphrase', 'pwd', 'secret', 'secrets',
  'credential', 'credentials', 'apikey', 'privatekey']);

/**
 * Секретный ли ключ: делим на слова по `_ - .` и camelCase, проверяем слова
 * и пары соседних («api_key» → apikey). «bypass», «passive» — не секреты.
 */
export function isSecretKey(key: string): boolean {
  const words = key.replace(/([a-z0-9])([A-Z])/g, '$1 $2').toLowerCase().split(/[\s_.-]+/).filter(Boolean);
  return words.some((w, i) => SECRET_WORDS.has(w) || SECRET_WORDS.has(w + (words[i + 1] ?? '')));
}

export function passwordView(d: Record<string, unknown>): string | null {
  if (d.password !== undefined) warnSecret();
  // Пришедший пароль — «скрыт» при любом флаге: ни значения, ни длины, ни краёв.
  if (d.password !== undefined || d.password_present === true) return 'пароль скрыт';
  if (d.password_present === false) return 'пароль не записан';
  return null;
}

/**
 * Краткое описание события — только текст (S1). Данные атакующего
 * очищаются от управляющих символов (S2), пароли маскируются (S4).
 * Результат выводится как текстовый узел, без разметки.
 */
export function describeEvent(e: Pick<HfEvent, 'type' | 'data'> & { service?: unknown }): string {
  const d = eventData(e);
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
    case 'tcp.request': {
      const methods = ['GET', 'HEAD', 'POST', 'PUT', 'DELETE', 'OPTIONS', 'PATCH', 'TRACE', 'CONNECT'];
      const method = typeof d.method === 'string' && methods.includes(d.method) ? d.method : 'метод не распознан';
      const path = d.path === '/' ? '/' : d.path == null ? '' : '[скрыто]';
      const capture = Number.isSafeInteger(d.captured_bytes) && Number(d.captured_bytes) >= 0
        ? `захвачено ${d.captured_bytes} байт; ` : '';
      const shortened = d.mapper_truncated === true ? '; сохранён префикс' : '';
      const prefix = e.type === 'http.request' ? `${method} ${path} — ` : '';
      return prefix + capture + 'полнота захвата неизвестна' + shortened
        + (e.type === 'http.request' ? '; ответ не записан' : '');
    }
    case 'connection.opened':
    case 'session.closed':
      // Полезных данных обычно нет — показываем сервис: «tcp :2222».
      return sanitizeTerminal(serviceLabel(e.service));
    case 'token.triggered':
      // URL приманки не делаем ссылкой и не открываем (S3).
      return sanitizeTerminal(`приманка ${str(d.token_id)}`);
    default: {
      // Неизвестный тип — данные текстом, но без секретов: пароль в нём — ошибка сервера (S4).
      if (!Object.keys(d).length) return '';
      // Секретные ключи убираем на любой глубине.
      const text = JSON.stringify(d, (k, v: unknown) => {
        if (k && isSecretKey(k)) { warnSecret(); return undefined; }
        return v;
      });
      return sanitizeTerminal(text === '{}' ? '' : text);
    }
  }
}
