import { eventKey, type HfEvent } from '../../shared/api/types';

/**
 * Окно событий ленты в памяти (specs, «Лента: фильтры и живой поток»).
 *
 * Разделяет загруженную по REST историю и живые события из потока:
 * - история растёт постранично («Показать ещё») и целиком сохраняется, чтобы
 *   старые страницы не вытеснялись лимитом (иначе их нельзя пролистать);
 * - живые события копятся сверху и ограничены `liveLimit`, чтобы при
 *   интенсивном потоке память и обработка не росли без предела;
 * - на паузе живые события не показываются, а копятся в `pending`, тоже
 *   ограниченном `liveLimit`.
 *
 * Уникальность события — пара `(agent_id, event_id)`: одинаковый `event_id`
 * от разных агентов считается разными событиями.
 */
export class FeedBuffer {
  private historyItems: HfEvent[] = [];
  private liveItems: HfEvent[] = [];
  private pendingItems: HfEvent[] = [];
  /** Ключи показанных событий (история + живые) для отсева повторов. */
  private keys = new Set<string>();
  /** Ключи отложенных на паузе событий — отдельно, чтобы не мешать показу. */
  private pendingKeys = new Set<string>();
  private paused = false;
  private readonly liveLimit: number;

  constructor(liveLimit = 500) { this.liveLimit = liveLimit; }

  /** Видимое окно: живые события сверху, затем история. */
  get events(): readonly HfEvent[] { return [...this.liveItems, ...this.historyItems]; }
  get pending(): number { return this.pendingItems.length; }
  get isPaused(): boolean { return this.paused; }

  /** Страница из REST: заменить окно (первая) или дописать историю снизу (следующие). */
  loadPage(page: HfEvent[], append: boolean) {
    if (!append) {
      this.historyItems = [];
      this.liveItems = [];
      this.pendingItems = [];
      this.keys.clear();
      this.pendingKeys.clear();
    }
    for (const e of page) {
      const k = eventKey(e);
      if (this.keys.has(k)) continue;
      this.keys.add(k);
      this.historyItems.push(e);
    }
  }

  /** Живые события сверху (самое новое — первым). На паузе уходят в pending. */
  pushLive(events: HfEvent[]) {
    const fresh = events.filter((e) => {
      const k = eventKey(e);
      return !this.keys.has(k) && !this.pendingKeys.has(k);
    });
    if (!fresh.length) return;
    if (this.paused) {
      for (const e of fresh) this.pendingKeys.add(eventKey(e));
      this.pendingItems.push(...fresh);
      this.boundPending();
      return;
    }
    this.prepend(fresh);
  }

  setPaused(paused: boolean) {
    this.paused = paused;
    if (!paused) this.flush();
  }

  /** Показать накопленное на паузе (пауза при этом остаётся, если была). */
  flush() {
    const p = this.pendingItems;
    this.pendingItems = [];
    this.pendingKeys.clear();
    if (p.length) this.prepend(p);
  }

  private prepend(events: HfEvent[]) {
    const ordered = [...events].reverse();
    for (const e of ordered) this.keys.add(eventKey(e));
    this.liveItems = [...ordered, ...this.liveItems];
    this.boundLive();
  }

  /** Живых событий — не больше лимита; лишние старые (снизу) забываем. */
  private boundLive() {
    if (this.liveItems.length <= this.liveLimit) return;
    for (const e of this.liveItems.slice(this.liveLimit)) this.keys.delete(eventKey(e));
    this.liveItems = this.liveItems.slice(0, this.liveLimit);
  }

  /** Отложенных на паузе — тоже не больше лимита; лишние старые забываем. */
  private boundPending() {
    if (this.pendingItems.length <= this.liveLimit) return;
    const drop = this.pendingItems.length - this.liveLimit;
    for (const e of this.pendingItems.slice(0, drop)) this.pendingKeys.delete(eventKey(e));
    this.pendingItems = this.pendingItems.slice(drop);
  }
}
