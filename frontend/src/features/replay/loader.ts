import { api, ApiError } from '../../shared/api/client';
import { ReplayTape, type ReplayChunk } from './tape';

/** Страница записи (specs/frontend.md, «Replay», загрузка). */
export interface ReplayPage {
  session_id: string;
  started_at: string;
  chunks: ReplayChunk[];
  next_cursor: string | null;
  tail_cursor: string | null;
  complete: boolean;
}

export type FetchReplay = (id: string, after: string | null) => Promise<ReplayPage>;

export const fetchReplay: FetchReplay = (id, after) => {
  const q = new URLSearchParams({ limit: '200' });
  if (after !== null) q.set('after', after);
  return api<ReplayPage>(`/api/v1/sessions/${encodeURIComponent(id)}/replay?${q}`);
};

/** Пауза между запросами хвоста, пока запись идёт. */
export const TAIL_MS = 2000;

export interface ReplayState {
  tape: ReplayTape;
  startedAt: string | null;
  /** Запись завершена и прочитана целиком. */
  complete: boolean;
  /** 404: записи нет. */
  notFound: boolean;
  /** Прочий сбой; уже показанное сохраняется, `retry()` продолжает с того же места. */
  error: unknown;
  /** Номер изменения — для useSyncExternalStore. */
  version: number;
}

/**
 * Загрузка записи сессии: страницы по `next_cursor`, затем, пока запись
 * идёт, хвост раз в TAIL_MS по `tail_cursor`. 422 (курсор устарел) —
 * перечитать с начала в новую ленту.
 */
export class ReplayLoader {
  private readonly id: string;
  private readonly fetch: FetchReplay;
  private readonly wait: (ms: number) => Promise<void>;
  private state: ReplayState = ReplayLoader.empty(0);
  private readonly listeners = new Set<() => void>();
  /** Курсор следующего запроса. */
  private after: string | null = null;
  /** Поколение цикла: stop/restart делают старые ответы недействительными. */
  private run = 0;

  constructor(id: string, fetch: FetchReplay = fetchReplay, wait = (ms: number) => new Promise<void>((r) => setTimeout(r, ms))) {
    this.id = id;
    this.fetch = fetch;
    this.wait = wait;
  }

  private static empty(version: number): ReplayState {
    return { tape: new ReplayTape(), startedAt: null, complete: false, notFound: false, error: null, version };
  }

  get snapshot(): ReplayState { return this.state; }

  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  };

  start() { void this.loop(++this.run); }

  stop() { this.run++; }

  /** Повтор после сбоя: с того же курсора, показанное остаётся. */
  retry() {
    this.set({ error: null });
    this.start();
  }

  private async loop(run: number) {
    while (run === this.run) {
      let page: ReplayPage;
      try {
        page = await this.fetch(this.id, this.after);
      } catch (error) {
        if (run !== this.run) return;
        if (error instanceof ApiError && error.status === 422) {
          // Курсор недействителен — читаем с начала в чистую ленту.
          this.after = null;
          this.state = ReplayLoader.empty(this.state.version);
          this.set({});
          continue;
        }
        if (error instanceof ApiError && error.status === 404) this.set({ notFound: true });
        else this.set({ error });
        return;
      }
      if (run !== this.run) return;
      this.state.tape.append(page.chunks);
      if (page.next_cursor !== null) {
        this.after = page.next_cursor;
        this.set({ startedAt: page.started_at });
        continue;
      }
      if (page.complete) {
        this.state.tape.finish();
        this.set({ startedAt: page.started_at, complete: true });
        return;
      }
      // Запись идёт: хвост с tail_cursor (null — без after; повторы seq лента пропустит).
      this.after = page.tail_cursor;
      this.set({ startedAt: page.started_at });
      await this.wait(TAIL_MS);
    }
  }

  private set(patch: Partial<ReplayState>) {
    this.state = { ...this.state, ...patch, version: this.state.version + 1 };
    for (const l of this.listeners) l();
  }
}
