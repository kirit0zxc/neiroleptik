import type { HfEvent } from '../../shared/api/types';

/**
 * Окно событий ленты в памяти (specs, «Лента: фильтры и живой поток»).
 *
 * Новые сверху. Повтор по event_id показывается один раз. Не больше
 * `limit` событий — старые вытесняются. На паузе живые события не
 * попадают в окно, а копятся в `pending` до «показать».
 */
export class FeedBuffer {
  private items: HfEvent[] = [];
  private ids = new Set<string>();
  private pendingItems: HfEvent[] = [];
  private paused = false;
  private readonly limit: number;

  constructor(limit = 500) { this.limit = limit; }

  get events(): readonly HfEvent[] { return this.items; }
  get pending(): number { return this.pendingItems.length; }
  get isPaused(): boolean { return this.paused; }

  /** Страница из REST: заменить окно (первая) или дописать снизу (следующие). */
  loadPage(page: HfEvent[], append: boolean) {
    if (!append) { this.items = []; this.ids.clear(); this.pendingItems = []; }
    for (const e of page) {
      if (this.ids.has(e.event_id)) continue;
      this.ids.add(e.event_id);
      this.items.push(e);
    }
    this.trim();
  }

  /** Живые события сверху (самое новое — первым). */
  pushLive(events: HfEvent[]) {
    const fresh = events.filter((e) => !this.ids.has(e.event_id) && !this.pendingItems.some((p) => p.event_id === e.event_id));
    if (!fresh.length) return;
    if (this.paused) { this.pendingItems.push(...fresh); return; }
    this.prepend(fresh);
  }

  setPaused(paused: boolean) {
    this.paused = paused;
    if (!paused) this.flush();
  }

  /** Показать накопленное на паузе, пауза остаётся. */
  flush() {
    const p = this.pendingItems;
    this.pendingItems = [];
    if (p.length) this.prepend(p);
  }

  private prepend(events: HfEvent[]) {
    const ordered = [...events].reverse();
    for (const e of ordered) this.ids.add(e.event_id);
    this.items = [...ordered, ...this.items];
    this.trim();
  }

  private trim() {
    if (this.items.length <= this.limit) return;
    for (const e of this.items.slice(this.limit)) this.ids.delete(e.event_id);
    this.items = this.items.slice(0, this.limit);
  }
}
