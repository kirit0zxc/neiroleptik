import type { MascotCore } from '../core/MascotCore';
import { hasState } from '../core/states';
import type { MascotState } from '../core/types';
import { normalize, parseEvent } from './normalize';
import { ORIGINAL_FETCH } from '../assets';
import type {
  BridgeOptions, BridgeRules, NormalizedPayload, ReactionSpec, ResolvedReaction,
  WrapFetchOptions,
} from './types';

const requestUrl = (input: RequestInfo | URL) =>
  typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;

const safeMatch = (match: (url: string) => boolean, url: string) => {
  try { return match(url); } catch { return false; }
};

/**
 * Запросы, которые никогда не бывают «запросом приложения»: blob: и data:
 * читаются из памяти. Через них, например, three.js грузит текстуры
 * самой Юми — без этого исключения она реагировала бы на себя.
 */
const isLocalUrl = (url: string) => /^(?:blob|data):/i.test(url);

/** Метка обёртки: повторный wrapFetch того же моста её не наращивает. */
const WRAPPED_BY = Symbol.for('mascot.wrappedBy');

const own = <T extends object>(table: T, key: string) =>
  Object.prototype.hasOwnProperty.call(table, key);

/**
 * Мост между пайплайном и маскотом.
 *
 * Весь смысл в том, чтобы подключение сводилось к таблице, а не
 * к правкам кода в десяти местах. Бэкенд шлёт события как умеет,
 * мост приводит их к общему виду и смотрит в таблицу.
 *
 * Отдельное требование: что бы ни пришло, приложение не падает.
 * Любое исключение внутри обработки гасится здесь и дальше не идёт.
 */
export class MascotBridge {
  private core: MascotCore;
  private rules: BridgeRules;
  private opts: BridgeOptions;
  /** -Infinity: иначе события в первые throttleMs после загрузки страницы откладывались бы. */
  private lastAt = -Infinity;
  private cleanups = new Set<() => void>();
  /** Реакция, отложенная throttle'ом: последняя всегда доходит. */
  private deferred: ResolvedReaction | null = null;
  private deferTimer: ReturnType<typeof setTimeout> | null = null;
  /** Сколько запросов через wrapFetch сейчас в полёте. */
  private inFlight = 0;
  /** Итоги текущей волны запросов. */
  private batch = { failed: false, succeeded: false, status: 0 };

  constructor(core: MascotCore, rules: BridgeRules, options: BridgeOptions = {}) {
    this.core = core;
    this.rules = rules;
    this.opts = options;
  }

  /** Добавить или переопределить правила на лету. */
  extend(rules: BridgeRules) {
    this.rules = { ...this.rules, ...rules };
  }

  /** Основной вход: имя события и произвольные данные. */
  handle(name: string, payload?: unknown) {
    try {
      this.dispatch(name, payload);
    } catch (error) {
      // Маскот — украшение, он не имеет права ронять приложение.
      console.warn('[mascot/bridge] событие не обработано', name, error);
    }
  }

  /** Принять «сырое» сообщение, из которого ещё надо достать имя. */
  handleRaw(input: unknown) {
    const parsed = parseEvent(input);
    if (!parsed) {
      if (this.opts.debug) console.debug('[mascot/bridge] не понял сообщение', input);
      return;
    }
    this.handle(parsed.name, parsed.payload);
  }

  private dispatch(name: string, payload: unknown) {
    if (this.opts.debug) console.debug('[mascot/bridge] ←', name, payload);

    // Только свои правила: имя события приходит из сети, и 'constructor'
    // или 'toString' не должны находиться в прототипе таблицы.
    const spec = (own(this.rules, name) ? this.rules[name] : undefined) ?? this.opts.fallback ?? null;
    if (spec == null) return;

    // Сначала разворачиваем правило: если оно решило не реагировать (null),
    // событие не должно вытеснить отложенную реакцию — например, done.
    const resolved = this.resolve(spec, normalize(payload));
    if (resolved == null) return;

    // Throttle с хвостом: промежуточные реакции схлопываются, но
    // последняя обязательно применится. Иначе шторм событий мог бы
    // проглотить финальный done или error, и Юми зависла бы в thinking.
    const now = performance.now();
    const gap = this.opts.throttleMs ?? 0;
    if (gap && now - this.lastAt < gap) {
      this.deferred = resolved;
      this.deferTimer ??= setTimeout(() => {
        this.deferTimer = null;
        const next = this.deferred;
        this.deferred = null;
        if (next) { this.lastAt = performance.now(); this.apply(next); }
      }, gap - (now - this.lastAt));
      return;
    }
    this.lastAt = now;
    this.apply(resolved);
  }

  /** Исполнить готовую реакцию. Жест — часть состояния, а не обход ядра. */
  private apply(resolved: ResolvedReaction) {
    try {
      if (typeof resolved === 'string') {
        this.applyState(resolved);
        return;
      }
      if (resolved.state) this.applyState(resolved.state);
      if (resolved.clip) this.core.playGesture(resolved.clip);
      if (resolved.say) this.core.say(resolved.say);
    } catch (error) {
      console.warn('[mascot/bridge] реакция не выполнена', resolved, error);
    }
  }

  /**
   * Разворачивает функции-реакции до конкретного действия.
   *
   * Функция может вернуть другую функцию — это удобно для общих
   * правил, но зациклиться на этом не хочется, поэтому глубина
   * ограничена.
   */
  private resolve(
    spec: ReactionSpec, data: NormalizedPayload, depth = 0,
  ): ResolvedReaction | null {
    if (typeof spec !== 'function') return spec;
    if (depth >= 3) {
      console.warn('[mascot/bridge] правило слишком глубоко вложено');
      return null;
    }
    const next = spec(data);
    return next == null ? null : this.resolve(next, data, depth + 1);
  }

  private applyState(state: MascotState) {
    // Незнакомое состояние — не повод падать, просто пропускаем.
    if (!hasState(state)) {
      console.warn('[mascot/bridge] неизвестное состояние:', state);
      return;
    }
    this.core.setState(state);
  }

  // ─── Источники событий ────────────────────────────────────

  /**
   * Подписаться и вернуть отписку только от этого источника.
   * dispose() снимает все подписки моста разом.
   */
  private subscribe(target: EventTarget, handlers: Record<string, (e: Event) => void>) {
    const entries = Object.entries(handlers);
    for (const [name, fn] of entries) target.addEventListener(name, fn);
    const off = () => {
      for (const [name, fn] of entries) target.removeEventListener(name, fn);
      this.cleanups.delete(off);
    };
    this.cleanups.add(off);
    return off;
  }

  /** Любой EventTarget: window, document, свой эмиттер. */
  listen(target: EventTarget, events: string[]) {
    const handlers: Record<string, (e: Event) => void> = {};
    for (const name of events) {
      handlers[name] = (e) => this.handle(name, (e as CustomEvent).detail);
    }
    return this.subscribe(target, handlers);
  }

  /** WebSocket: каждое сообщение разбирается автоматически. */
  listenWebSocket(ws: WebSocket) {
    // На 'error' не подписываемся: за ним браузер всегда шлёт 'close',
    // и Юми огорчалась бы дважды подряд.
    return this.subscribe(ws, {
      message: (e) => this.handleRaw((e as MessageEvent).data),
      open: () => this.handle('ws:open'),
      // Код и «чистота» закрытия — правило отличит уход со страницы от обрыва.
      close: (e) => {
        const { code, wasClean } = e as CloseEvent;
        this.handle('ws:close', { code, wasClean });
      },
    });
  }

  /** Server-Sent Events — так был устроен стрим в прошлой Yumi. */
  listenEventSource(es: EventSource, namedEvents: string[] = []) {
    const handlers: Record<string, (e: Event) => void> = {
      message: (e) => this.handleRaw((e as MessageEvent).data),
    };
    for (const name of namedEvents) {
      handlers[name] = (e) => this.handle(name, (e as MessageEvent).data);
    }
    return this.subscribe(es, handlers);
  }

  /**
   * Обёртка над fetch: запрос сам переводит Юми в thinking,
   * а ответ — в success или error.
   *
   * Это самый дешёвый способ интеграции: одна строка подмены,
   * и маскот уже отражает происходящее, без единого вызова руками.
   * Собственные файлы Юми грузит мимо этой обёртки.
   */
  wrapFetch(base: typeof fetch = fetch, options: WrapFetchOptions = {}): typeof fetch {
    // Повторный вызов (HMR, StrictMode) не должен наращивать цепочку
    // обёрток: каждая считала бы ту же волну, и события дублировались бы.
    if ((base as typeof fetch & { [WRAPPED_BY]?: unknown })[WRAPPED_BY] === this) return base;
    // Нативный fetch, вызванный не от window, бросает Illegal invocation.
    const call: typeof fetch = base === globalThis.fetch ? base.bind(globalThis) : base;
    const { match } = options;

    const wrapped = async (...args: Parameters<typeof fetch>) => {
      const url = requestUrl(args[0]);
      if (isLocalUrl(url) || (match && !safeMatch(match, url))) return call(...args);

      // Параллельные запросы — одна волна: thinking на первом,
      // итог — когда вернулся последний.
      if (this.inFlight++ === 0) {
        this.batch = { failed: false, succeeded: false, status: 0 };
        this.handle('request:start');
      }
      let res: Response;
      try {
        res = await call(...args);
      } catch (error) {
        // Отменённый запрос (AbortController) — не ошибка.
        const aborted = error instanceof DOMException && error.name === 'AbortError';
        this.settle(aborted ? { kind: 'abort' } : { kind: 'error', detail: { message: String(error) } });
        throw error;
      }
      // Непрозрачный ответ (mode: 'no-cors', redirect: 'manual') всегда
      // ok=false и status=0: судить о нём нельзя — считаем, что всё в порядке.
      const opaque = res.type === 'opaque' || res.type === 'opaqueredirect';
      this.settle(res.ok || opaque
        ? { kind: 'ok', status: res.status }
        : { kind: 'error', detail: { status: res.status } });
      return res;
    };
    // Метка для загрузчика ассетов: свои файлы Юми грузит мимо обёртки,
    // даже если её поставили раньше, чем загрузился маскот.
    return Object.assign(wrapped, { [ORIGINAL_FETCH]: call, [WRAPPED_BY]: this });
  }

  /** Запрос вернулся — подводим итог волны, когда вернулся последний. */
  private settle(outcome:
    | { kind: 'ok'; status: number }
    | { kind: 'error'; detail: Record<string, unknown> }
    | { kind: 'abort' },
  ) {
    this.inFlight = Math.max(0, this.inFlight - 1);
    if (outcome.kind === 'error') {
      // Об ошибке говорим сразу, не дожидаясь остальных запросов.
      this.batch.failed = true;
      this.handle('request:error', outcome.detail);
      return;
    }
    if (outcome.kind === 'ok') {
      this.batch.succeeded = true;
      this.batch.status = outcome.status;
    }
    if (this.inFlight > 0 || this.batch.failed) return;
    // Всё отменили — это не успех, но и не повод застревать в thinking.
    if (this.batch.succeeded) this.handle('request:done', { status: this.batch.status });
    else this.handle('request:cancel');
  }

  dispose() {
    for (const off of [...this.cleanups]) off();
    if (this.deferTimer) { clearTimeout(this.deferTimer); this.deferTimer = null; }
    this.deferred = null;
  }
}
