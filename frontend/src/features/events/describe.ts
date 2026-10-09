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
      return sanitizeTerminal(`${str(d.username)} / ${maskSecret(str(d.password))} ${ok}`.trim());
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
