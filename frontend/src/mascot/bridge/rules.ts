import type { BridgeRules } from './types';

/**
 * Готовая таблица под типовой ML-пайплайн.
 *
 * Имена событий намеренно разные по стилю: бэкенды называют одно
 * и то же по-своему, и проще покрыть распространённые варианты
 * сразу, чем договариваться под часами в день хакатона.
 */
export const DEFAULT_RULES: BridgeRules = {
  // Запрос ушёл в модель
  'request:start': 'thinking',
  'inference:start': 'thinking',
  'predict:start': 'thinking',
  loading: 'thinking',

  // Поиск и подбор
  'search:start': 'searching',
  'retrieve:start': 'searching',

  // Разбор результата
  'analyze:start': 'analyzing',
  'compare:start': 'comparing',

  // Готово — реакция зависит от уверенности модели
  'request:done': (p) => {
    if (!p.has('confidence')) return 'success';
    const c = p.num('confidence', 1);
    if (c < 0.4) return { state: 'curious', say: 'Я не уверена в этом результате.' };
    if (c < 0.7) return 'gentle';
    return 'success';
  },
  'inference:done': (p) => (p.num('confidence', 1) < 0.5 ? 'curious' : 'success'),
  'predict:done': 'success',
  done: 'success',
  ready: 'success',

  // Сбои
  // Текст ошибки не озвучиваем: там бывает «TypeError: Failed to fetch»
  // или служебные детали бэкенда. Он остаётся в payload для своих правил.
  'request:error': { state: 'error', say: 'Что-то пошло не так. Попробуем ещё раз?' },
  error: 'error',
  failed: 'error',
  'request:retry': 'retry',
  // Запрос отменили (AbortController) — просто успокоиться
  'request:cancel': 'idle',
  retry: 'retry',

  // Связь
  'ws:open': 'welcome',
  // Обычное закрытие (уход со страницы, код 1000) — не повод грустить.
  'ws:close': (p) => (p.bool('wasClean') && p.num('code') === 1000 ? 'idle' : 'error'),
  connected: 'welcome',
  disconnected: 'error',

  // Человек что-то ввёл
  'user:input': 'curious',
  'user:submit': 'thinking',
};
