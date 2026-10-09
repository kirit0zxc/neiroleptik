// @vitest-environment happy-dom
import { act, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { App, makeQueryClient } from '../../app/App';
import { handlers, resetMockSession } from '../../mocks/handlers';
import { nextDemoEvent, publish } from '../../mocks/stream';
import type { HfEvent } from '../../shared/api/types';
import { StreamClient, type StreamSocket } from '../../shared/stream/StreamClient';

const server = setupServer(...handlers);
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => server.resetHandlers());
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
  beforeEach(() => resetMockSession(true));

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
    s.send({ ...nextDemoEvent(), event_id: 'live-overview', type: 'session.command', data: { input: 'whoami' } });
    expect(within(screen.getAllByTestId('recent-row')[0]!).getByText('whoami')).toBeInTheDocument();
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
