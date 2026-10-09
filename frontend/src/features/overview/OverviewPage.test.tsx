// @vitest-environment happy-dom
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { App, makeQueryClient } from '../../app/App';
import { handlers, resetMockSession } from '../../mocks/handlers';
import { nextDemoEvent, publish } from '../../mocks/stream';
import type { HfEvent } from '../../shared/api/types';
import { ApiError } from '../../shared/api/client';
import { StreamClient, type StreamSocket } from '../../shared/stream/StreamClient';

const server = setupServer(...handlers);
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => { server.resetHandlers(); vi.unstubAllEnvs(); });
afterAll(() => server.close());

/** Поток под управлением теста (hello сразу после подключения). */
function controlledStream() {
  let socket: StreamSocket | null = null;
  let seq = 1000;
  const create = () => new StreamClient<HfEvent>({
    connect: () => {
      socket = { onopen: null, onmessage: null, onclose: null, onerror: null, close: () => {} };
      queueMicrotask(() => socket?.onmessage?.({ data: JSON.stringify({ type: 'hello', watermark: String(seq) }) }));
      return socket;
    },
    fetchGap: async (_a, until) => ({ items: [], watermark: until, next_cursor: null }),
  });
  const send = (e: HfEvent) => act(() => {
    seq += 1;
    socket!.onmessage?.({ data: JSON.stringify({ type: 'event', stream_seq: String(seq), event: e }) });
  });
  return { create, send };
}

function open(create?: () => StreamClient<HfEvent>) {
  window.history.pushState({}, '', '/');
  render(<App client={makeQueryClient()} createStream={create} />);
}

describe('обзор', () => {
  beforeEach(() => { vi.stubEnv('VITE_API_MOCK', '1'); resetMockSession(true); });

  it('плитки: ловушки, хосты, алерты — с предупреждениями и ссылками', async () => {
    open(controlledStream().create);
    const traps = await screen.findByRole('link', { name: /^Ловушки:/ });
    expect(traps).toHaveAccessibleName('Ловушки: 2 из 3 работают, упали: 1');
    expect(traps).toHaveAttribute('href', '/fleet');
    expect(screen.getByRole('link', { name: /^Хосты:/ })).toHaveAccessibleName('Хосты: 2 из 3 на связи, offline: 1');
    expect(screen.getByRole('link', { name: /^Открытые алерты:/ })).toHaveAttribute('href', '/alerts');
  });

  it('новое событие из потока встаёт первым в «Последних событиях»', async () => {
    const s = controlledStream();
    open(s.create);
    await screen.findAllByTestId('recent-row');
    await screen.findByText('онлайн');
    s.send({ ...nextDemoEvent(), event_id: 'live-overview', type: 'session.command', data: { input: 'whoami' } });
    expect(within(screen.getAllByTestId('recent-row')[0]!).getByText('whoami')).toBeInTheDocument();
  });

  it.each([null, '5'])('обзор обновляет снимок после hello при начальном курсоре %s', async (cursor) => {
    const event = (id: string) => ({ ...nextDemoEvent(), event_id: id, type: 'session.command', data: { input: `overview-${id}` } });
    let reads = 0;
    let release!: () => void;
    const waiting = new Promise<void>((resolve) => { release = resolve; });
    server.use(http.get('/api/v1/events', async () => {
      reads += 1;
      if (reads > 1) await waiting;
      return HttpResponse.json({ items: reads === 1 ? [event('9')] : [event('10'), event('9')], next_cursor: null });
    }));
    const sockets: StreamSocket[] = [];
    const timers: Array<() => void> = [];
    let stream!: StreamClient<HfEvent>;
    const create = () => (stream = new StreamClient<HfEvent>({
      cursor,
      connect: () => {
        const socket: StreamSocket = { onopen: null, onmessage: null, onclose: null, onerror: null, close: () => socket.onclose?.({ code: 1000 }) };
        sockets.push(socket); return socket;
      },
      fetchGap: async () => { throw new ApiError(410, 'История утрачена'); },
      isGone: (error) => error instanceof ApiError && error.status === 410,
      setTimer: (fn) => { timers.push(fn); return timers.length; }, clearTimer: () => {},
    }));
    try {
      open(create);
      await screen.findByText('overview-9');
      if (cursor !== null) {
        act(() => sockets[0]!.onmessage?.({ data: JSON.stringify({ type: 'hello', watermark: '9' }) }));
        await waitFor(() => expect(timers).toHaveLength(1));
        expect(reads).toBe(1);
        act(() => timers[0]!());
      }
      const socket = sockets.at(-1)!;
      act(() => socket.onmessage?.({ data: JSON.stringify({ type: 'hello', watermark: '10' }) }));
      await waitFor(() => expect(reads).toBe(2));
      act(() => socket.onmessage?.({ data: JSON.stringify({ type: 'event', stream_seq: '11', event: event('11') }) }));
      expect(screen.queryByText('overview-11')).toBeNull();
      release();
      expect(await screen.findByText('overview-10')).toBeInTheDocument();
      expect(await screen.findByText('overview-11')).toBeInTheDocument();
      expect(stream.currentCursor).toBe('11');
    } finally { release(); }
  });

  it('активные источники ведут в ленту с фильтром по IP', async () => {
    const user = userEvent.setup();
    publish(nextDemoEvent());
    publish(nextDemoEvent());
    open(controlledStream().create);
    const link = await screen.findByRole('link', { name: '203.0.113.50' });
    await user.click(link);
    expect(window.location.pathname + window.location.search).toBe('/events?source_ip=203.0.113.50');
  });

  it('сводка не загрузилась — ошибка с «повторить», последние события работают', async () => {
    server.use(http.get('/api/v1/analytics/summary', () => HttpResponse.json({ detail: 'сбой аналитики' }, { status: 500 })));
    open(controlledStream().create);
    // 5xx повторяется дважды с паузой (так задано в QueryClient) — ждём итог.
    expect(await screen.findByText('сбой аналитики', {}, { timeout: 8000 })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Повторить' })).toBeInTheDocument();
    expect(await screen.findAllByTestId('recent-row')).toHaveLength(3);
  }, 15_000);
});
