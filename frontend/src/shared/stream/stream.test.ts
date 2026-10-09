// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from 'vitest';
import { createEventStream } from './stream';

/** Браузерный транспорт; настоящий адаптер строит URL и обрабатывает сообщения. */
class TestSocket {
  static instances: TestSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor() { TestSocket.instances.push(this); }
  hello(watermark: string) { this.onmessage?.({ data: JSON.stringify({ type: 'hello', watermark }) }); }
  close() { this.onclose?.({ code: 1000 }); }
}

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); TestSocket.instances = []; });

it('настоящий адаптер догружает больше страницы через after при неизменном until', async () => {
  vi.useFakeTimers();
  vi.stubGlobal('WebSocket', TestSocket);
  const requests: URL[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    const url = new URL(input, location.origin);
    requests.push(url);
    expect(url.pathname).toBe('/api/v1/stream-events');
    expect(url.searchParams.has('cursor')).toBe(false);
    expect(url.searchParams.get('until')).toBe('350');
    const after = Number(url.searchParams.get('after'));
    const end = Math.min(after + 100, 350);
    const items = Array.from({ length: end - after }, (_, i) => ({
      stream_seq: String(after + i + 1), event: { agent_id: 'agent', event_id: `e${after + i + 1}` },
    }));
    return new Response(JSON.stringify({ items, watermark: '350', next_cursor: end < 350 ? String(end) : null }),
      { headers: { 'Content-Type': 'application/json' } });
  }));
  const stream = createEventStream();
  const got: string[] = [];
  stream.subscribe((records) => got.push(...records.map((r) => r.stream_seq)));
  stream.start();
  TestSocket.instances[0]!.hello('0');
  await vi.advanceTimersByTimeAsync(0);
  TestSocket.instances[0]!.close();
  await vi.advanceTimersByTimeAsync(1000);
  TestSocket.instances[1]!.hello('350');
  await vi.advanceTimersByTimeAsync(0);
  expect(requests.map((u) => u.searchParams.get('after'))).toEqual(['0', '100', '200', '300']);
  expect(got).toHaveLength(350);
  expect(got[0]).toBe('1');
  expect(got.at(-1)).toBe('350');
  expect(stream.currentCursor).toBe('350');
  expect(stream.currentStatus).toBe('live');
  stream.stop();
});
