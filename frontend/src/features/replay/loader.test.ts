import { describe, expect, it, vi } from 'vitest';
import { ApiError } from '../../shared/api/client';
import { ReplayLoader, type ReplayPage } from './loader';
import type { ReplayChunk } from './tape';

const chunk = (seq: number, text: string): ReplayChunk => ({
  seq, offset_ms: seq * 10, direction: 'out', truncated: false, data_base64: btoa(text),
});
const page = (chunks: ReplayChunk[], patch: Partial<ReplayPage> = {}): ReplayPage => ({
  session_id: 's1', started_at: '2026-10-10T10:00:00Z', chunks, next_cursor: null, tail_cursor: null, complete: true, ...patch,
});
const text = (l: ReplayLoader) => l.snapshot.tape.segments.map((s) => ('gap' in s ? '|' : s.text)).join('');
const flush = () => new Promise((r) => setTimeout(r, 0));

/** Сервер-сценарий: ответы по очереди, запросы записываются. */
function server(...answers: (ReplayPage | Error)[]) {
  const calls: (string | null)[] = [];
  const fetch = vi.fn(async (_id: string, after: string | null) => {
    calls.push(after);
    const a = answers.shift();
    if (!a) throw new Error('лишний запрос');
    if (a instanceof Error) throw a;
    return a;
  });
  return { fetch, calls };
}

describe('Replay: загрузка (F15)', () => {
  it('страницы догружаются по next_cursor до конца', async () => {
    const s = server(page([chunk(0, 'a')], { next_cursor: 'c1' }), page([chunk(1, 'b')]));
    const l = new ReplayLoader('s1', s.fetch, async () => {});
    l.start(); await flush();
    expect(s.calls).toEqual([null, 'c1']);
    expect(text(l)).toBe('ab');
    expect(l.snapshot.complete).toBe(true);
    expect(l.snapshot.startedAt).toBe('2026-10-10T10:00:00Z');
  });

  it('запись идёт: хвост по tail_cursor после паузы, null — без after', async () => {
    const waits: number[] = [];
    const s = server(
      page([], { complete: false }),
      page([chunk(0, 'a')], { complete: false, tail_cursor: 't0' }),
      page([chunk(1, 'b')], { complete: true }),
    );
    const l = new ReplayLoader('s1', s.fetch, async (ms) => { waits.push(ms); });
    l.start(); await flush();
    expect(s.calls).toEqual([null, null, 't0']);
    expect(waits).toEqual([2000, 2000]);
    expect(text(l)).toBe('ab');
    expect(l.snapshot.complete).toBe(true);
  });

  it('422: запись перечитывается с начала в чистую ленту', async () => {
    const s = server(page([chunk(0, 'a')], { next_cursor: 'c1' }), new ApiError(422, 'bad'), page([chunk(0, 'A'), chunk(1, 'B')]));
    const l = new ReplayLoader('s1', s.fetch, async () => {});
    l.start(); await flush();
    expect(s.calls).toEqual([null, 'c1', null]);
    expect(text(l)).toBe('AB');
  });

  it('404 — записи нет', async () => {
    const l = new ReplayLoader('s1', server(new ApiError(404, 'нет')).fetch, async () => {});
    l.start(); await flush();
    expect(l.snapshot.notFound).toBe(true);
  });

  it('сбой: показанное остаётся, повтор с того же курсора', async () => {
    const s = server(page([chunk(0, 'a')], { next_cursor: 'c1' }), new ApiError(0, 'сеть'), page([chunk(1, 'b')]));
    const l = new ReplayLoader('s1', s.fetch, async () => {});
    l.start(); await flush();
    expect(l.snapshot.error).toBeInstanceOf(ApiError);
    expect(text(l)).toBe('a');
    l.retry(); await flush();
    expect(s.calls).toEqual([null, 'c1', 'c1']);
    expect(text(l)).toBe('ab');
    expect(l.snapshot.error).toBeNull();
  });

  it('после stop поздний ответ не меняет состояние', async () => {
    let resolve!: (p: ReplayPage) => void;
    const l = new ReplayLoader('s1', () => new Promise((r) => { resolve = r; }), async () => {});
    l.start(); l.stop();
    resolve(page([chunk(0, 'a')]));
    await flush();
    expect(text(l)).toBe('');
    expect(l.snapshot.complete).toBe(false);
  });
});
