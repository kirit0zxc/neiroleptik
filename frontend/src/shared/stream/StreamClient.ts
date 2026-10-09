/**
 * Живой поток событий с восстановлением после обрыва (specs/frontend.md, раздел 5).
 *
 * Протокол (согласован с командой 09.10):
 *   WS → {"type":"hello","watermark":"120"}
 *   WS → {"type":"event","stream_seq":"121","event":{…}}
 *   REST догрузка: after=<курсор>&until=<watermark>, постранично по next_cursor.
 * stream_seq и watermark — строки; сравниваем как BigInt, чтобы не терять
 * точность. Курсор сдвигается только после передачи подписчикам.
 *
 * Сеть и таймеры передаются снаружи — класс проверяется без браузера.
 */

/** Запись потока: событие и его номер публикации (строка с целым числом). */
export interface StreamRecord<E> {
  stream_seq: string;
  event: E;
}

/** Страница догрузки разрыва. */
export interface GapPage<E> {
  items: StreamRecord<E>[];
  watermark: string;
  next_cursor: string | null;
}

/** Минимальный интерфейс сокета — браузерный WebSocket через адаптер или подделка в тестах. */
export interface StreamSocket {
  onopen: (() => void) | null;
  onmessage: ((ev: { data: string }) => void) | null;
  onclose: ((ev: { code: number }) => void) | null;
  onerror: (() => void) | null;
  close(): void;
}

export type StreamStatus = 'idle' | 'connecting' | 'live' | 'reconnecting' | 'forbidden' | 'stopped';

/** Коды закрытия, согласованные с backend. */
export const CLOSE_UNAUTHORIZED = 4401;
export const CLOSE_FORBIDDEN = 4403;

export interface StreamClientOptions<E> {
  connect: () => StreamSocket;
  /** Догрузка (after, until] постранично; page — next_cursor прошлой страницы. */
  fetchGap: (after: string, until: string, page: string | null) => Promise<GapPage<E>>;
  /** Сессия истекла (4401) — приложение уводит на вход. */
  onUnauthorized?: () => void;
  /** Нештатный обрыв (1006 и т. п.) — приложение проверяет сессию через /auth/me. */
  onAbnormalClose?: () => void;
  /** Начальный курсор; null — первое подключение, история берётся из GET /events. */
  cursor?: string | null;
  setTimer?: (fn: () => void, ms: number) => unknown;
  clearTimer?: (id: unknown) => void;
}

const MAX_BACKOFF_MS = 30_000;
/** Сколько ID доставленных событий помним для отсева повторов. */
const SEEN_LIMIT = 5000;

const big = (s: string) => BigInt(s);

export class StreamClient<E extends { event_id: string }> {
  private readonly opts: StreamClientOptions<E>;
  private cursor: string | null;
  private socket: StreamSocket | null = null;
  /** События сокета, пришедшие до конца догрузки. */
  private buffer: StreamRecord<E>[] = [];
  private syncing = false;
  private delivered = new Set<string>();
  private listeners = new Set<(records: StreamRecord<E>[]) => void>();
  private statusListeners = new Set<(s: StreamStatus) => void>();
  private status: StreamStatus = 'idle';
  private attempt = 0;
  private timer: unknown = null;
  private readonly setTimer: (fn: () => void, ms: number) => unknown;
  private readonly clearTimer: (id: unknown) => void;

  constructor(opts: StreamClientOptions<E>) {
    this.opts = opts;
    this.cursor = opts.cursor ?? null;
    this.setTimer = opts.setTimer ?? ((fn, ms) => setTimeout(fn, ms));
    this.clearTimer = opts.clearTimer ?? ((id) => clearTimeout(id as ReturnType<typeof setTimeout>));
  }

  get currentCursor() { return this.cursor; }
  get currentStatus() { return this.status; }

  subscribe(fn: (records: StreamRecord<E>[]) => void) {
    this.listeners.add(fn);
    return () => { this.listeners.delete(fn); };
  }

  onStatus(fn: (s: StreamStatus) => void) {
    this.statusListeners.add(fn);
    return () => { this.statusListeners.delete(fn); };
  }

  start() {
    if (this.status !== 'idle' && this.status !== 'stopped') return;
    this.open();
  }

  stop() {
    if (this.timer !== null) { this.clearTimer(this.timer); this.timer = null; }
    this.dropSocket();
    this.setStatus('stopped');
  }

  // ─── Подключение ──────────────────────────────────────────

  private open() {
    this.setStatus(this.attempt === 0 ? 'connecting' : 'reconnecting');
    const socket = this.opts.connect();
    this.socket = socket;
    this.buffer = [];
    this.syncing = true;

    socket.onmessage = (ev) => {
      if (socket !== this.socket) return;
      const msg = parseMessage<E>(ev.data);
      if (!msg) return;
      if (msg.type === 'hello') { void this.sync(socket, msg.watermark); return; }
      if (this.syncing) this.buffer.push(msg.record);
      else this.deliver([msg.record]);
    };
    socket.onclose = (ev) => { if (socket === this.socket) this.handleClose(ev.code); };
    socket.onerror = () => { if (socket === this.socket) socket.close(); };
  }

  /** Шаги 2–5: догрузить (курсор, watermark], слить с буфером, передать. */
  private async sync(socket: StreamSocket, watermark: string) {
    try {
      const gap: StreamRecord<E>[] = [];
      if (this.cursor === null) {
        // Первое подключение: историю показывает GET /events, поток — с watermark.
        this.cursor = watermark;
      } else if (big(watermark) > big(this.cursor)) {
        let page: string | null = null;
        do {
          const res: GapPage<E> = await this.opts.fetchGap(this.cursor, watermark, page);
          if (socket !== this.socket) return;
          gap.push(...res.items);
          page = res.next_cursor;
        } while (page);
      }
      this.syncing = false;
      const pending = [...gap, ...this.buffer];
      this.buffer = [];
      this.deliver(pending);
      this.attempt = 0;
      this.setStatus('live');
    } catch {
      // Догрузка не удалась — переподключаемся, курсор не тронут.
      if (socket === this.socket) socket.close();
    }
  }

  private handleClose(code: number) {
    if (code === CLOSE_UNAUTHORIZED) {
      this.stop();
      this.opts.onUnauthorized?.();
      return;
    }
    if (code === CLOSE_FORBIDDEN) {
      this.dropSocket();
      this.setStatus('forbidden');
      return;
    }
    if (code !== 1000) this.opts.onAbnormalClose?.();
    this.scheduleReconnect();
  }

  private scheduleReconnect() {
    this.dropSocket();
    if (this.status === 'stopped') return;
    this.attempt += 1;
    this.setStatus('reconnecting');
    const delay = Math.min(1000 * 2 ** (this.attempt - 1), MAX_BACKOFF_MS);
    this.timer = this.setTimer(() => { this.timer = null; this.open(); }, delay);
  }

  private dropSocket() {
    const s = this.socket;
    this.socket = null;
    if (s) {
      s.onopen = null; s.onmessage = null; s.onclose = null; s.onerror = null;
      s.close();
    }
  }

  // ─── Доставка ─────────────────────────────────────────────

  private deliver(records: StreamRecord<E>[]) {
    const floor = this.cursor === null ? null : big(this.cursor);
    const fresh: StreamRecord<E>[] = [];
    const seen = new Set<string>();
    const sorted = [...records].sort((a, b) => (big(a.stream_seq) < big(b.stream_seq) ? -1 : 1));
    for (const r of sorted) {
      if (floor !== null && big(r.stream_seq) <= floor) continue;
      const id = r.event.event_id;
      if (this.delivered.has(id) || seen.has(id)) continue;
      seen.add(id);
      fresh.push(r);
    }
    if (!fresh.length) return;
    for (const fn of this.listeners) fn(fresh);
    for (const r of fresh) this.delivered.add(r.event.event_id);
    this.cursor = fresh.at(-1)!.stream_seq;
    if (this.delivered.size > SEEN_LIMIT) this.delivered = new Set(fresh.map((r) => r.event.event_id));
  }

  private setStatus(s: StreamStatus) {
    if (this.status === s) return;
    this.status = s;
    for (const fn of this.statusListeners) fn(s);
  }
}

type Message<E> = { type: 'hello'; watermark: string } | { type: 'event'; record: StreamRecord<E> };

const INT = /^\d+$/;

/** Разобрать сообщение сокета; мусор и неизвестные типы отбрасываются. */
export function parseMessage<E extends { event_id: string }>(data: string): Message<E> | null {
  let raw: Record<string, unknown>;
  try { raw = JSON.parse(data) as Record<string, unknown>; } catch { return null; }
  if (!raw || typeof raw !== 'object') return null;
  if (raw.type === 'hello') {
    return typeof raw.watermark === 'string' && INT.test(raw.watermark) ? { type: 'hello', watermark: raw.watermark } : null;
  }
  if (raw.type === 'event') {
    const event = raw.event as { event_id?: unknown } | undefined;
    if (typeof raw.stream_seq !== 'string' || !INT.test(raw.stream_seq)) return null;
    if (!event || typeof event.event_id !== 'string' || !event.event_id) return null;
    return { type: 'event', record: { stream_seq: raw.stream_seq, event: event as E } };
  }
  return null;
}
