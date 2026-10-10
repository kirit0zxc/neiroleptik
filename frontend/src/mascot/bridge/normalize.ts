import type { NormalizedPayload } from './types';

/** Достаёт значение по пути 'a.b.c', не падая ни на чём. */
function dig(src: unknown, path: string): unknown {
  if (src == null) return undefined;
  let cur: unknown = src;
  for (const key of path.split('.')) {
    if (cur == null || typeof cur !== 'object') return undefined;
    cur = (cur as Record<string, unknown>)[key];
  }
  return cur;
}

/**
 * Оборачивает любые входные данные в безопасный доступ.
 *
 * Главное требование к фронту маскота: что бы ни прислал бэкенд —
 * null, строку вместо объекта, недогруженный JSON, — приложение
 * не должно падать. Здесь всё приводится к типу или к запасному
 * значению, без исключений наружу.
 */
export function normalize(raw: unknown): NormalizedPayload {
  return {
    raw,
    num(path, fallback = 0) {
      const v = dig(raw, path);
      const n = typeof v === 'string' ? Number(v) : v;
      return typeof n === 'number' && Number.isFinite(n) ? n : fallback;
    },
    str(path, fallback = '') {
      const v = dig(raw, path);
      if (typeof v === 'string') return v;
      if (typeof v === 'number' || typeof v === 'boolean') return String(v);
      return fallback;
    },
    bool(path, fallback = false) {
      const v = dig(raw, path);
      if (typeof v === 'boolean') return v;
      if (v === 'true' || v === 1) return true;
      if (v === 'false' || v === 0) return false;
      return fallback;
    },
    has(path) {
      return dig(raw, path) !== undefined;
    },
  };
}

/** Имя события и полезная нагрузка, вытащенные из чего угодно. */
export interface ParsedEvent {
  name: string;
  payload: unknown;
}

/**
 * Приводит сообщение к паре «имя события + данные».
 *
 * Разные бэкенды называют поле по-разному: type, event, name, action.
 * Вместо того чтобы требовать один формат, разбираем все
 * распространённые — интеграция не должна упираться в соглашение
 * об имени поля.
 */
export function parseEvent(input: unknown): ParsedEvent | null {
  if (typeof input === 'string') {
    const text = input.trim();
    if (!text) return null;
    // Может прийти как голая строка-имя, так и JSON в строке.
    if (text.startsWith('{') || text.startsWith('[')) {
      try { return parseEvent(JSON.parse(text)); } catch { return { name: text, payload: null }; }
    }
    return { name: text, payload: null };
  }

  if (!input || typeof input !== 'object') return null;
  const o = input as Record<string, unknown>;

  for (const key of ['type', 'event', 'name', 'action', 'status', 'state']) {
    const v = o[key];
    if (typeof v === 'string' && v) {
      return { name: v, payload: o.payload ?? o.data ?? o };
    }
  }
  return null;
}
