import type { MascotState } from '../core/types';

/**
 * Что Юми делает в ответ на событие.
 *
 * Можно коротко — именем состояния; можно подробно — состоянием,
 * репликой и жестом; можно функцией, если реакция зависит от данных
 * (например, от уверенности модели).
 */
export interface ReactionDetail {
  state?: MascotState;
  say?: string;
  clip?: string;
}

/** Реакция после разворачивания функций — то, что уже можно исполнить. */
export type ResolvedReaction = MascotState | ReactionDetail;

export type ReactionSpec =
  | ResolvedReaction
  | ((payload: NormalizedPayload) => ReactionSpec | null | undefined);

/** Таблица «событие пайплайна → реакция». Это и есть вся интеграция. */
export type BridgeRules = Record<string, ReactionSpec>;

/**
 * Данные события, приведённые к безопасному виду.
 *
 * Бэкенд может прислать что угодно, включая мусор и null. Правила
 * читают payload через этот объект и не падают ни на чём.
 */
export interface NormalizedPayload {
  raw: unknown;
  /** Достать число по пути вида 'result.confidence'. */
  num(path: string, fallback?: number): number;
  /** Достать строку. */
  str(path: string, fallback?: string): string;
  /** Достать булево. */
  bool(path: string, fallback?: boolean): boolean;
  /** Есть ли что-то по пути. */
  has(path: string): boolean;
}

export interface WrapFetchOptions {
  /**
   * Какие запросы двигают Юми; по умолчанию все.
   * Например: (url) => url.includes('/api/predict')
   */
  match?: (url: string) => boolean;
}

export interface BridgeOptions {
  /** Реакция на событие, которого нет в таблице. null — игнорировать. */
  fallback?: ReactionSpec | null;
  /** Писать в консоль всё, что приходит. */
  debug?: boolean;
  /** Не реагировать чаще, чем раз в N мс. Защита от шторма событий. */
  throttleMs?: number;
}
