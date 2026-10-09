// @vitest-environment happy-dom
import { act, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { App, makeQueryClient } from '../../app/App';
import { handlers, resetMockSession } from '../../mocks/handlers';
import { publish } from '../../mocks/stream';
import type { HfEvent } from '../../shared/api/types';
import { StreamClient, type StreamSocket } from '../../shared/stream/StreamClient';

const server = setupServer(...handlers);
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

/** Поток, которым управляет тест: открыть сокет и прислать запись. */
function controlledStream() {
  let socket: StreamSocket | null = null;
  let seq = 100;
  const create = () => new StreamClient<HfEvent>({
    connect: () => {
      socket = { onopen: null, onmessage: null, onclose: null, onerror: null, close: () => {} };
      // Сервер сначала шлёт hello с watermark (specs, раздел 5).
      queueMicrotask(() => socket?.onmessage?.({ data: JSON.stringify({ type: 'hello', watermark: String(seq) }) }));
      return socket;
    },
    fetchGap: async (_after, until) => ({ items: [], watermark: until, next_cursor: null }),
  });
  const send = (e: HfEvent) => act(() => {
    seq += 1;
    socket!.onmessage?.({ data: JSON.stringify({ type: 'event', stream_seq: String(seq), event: e }) });
  });
  return { create, send };
}

const ev = (id: string, patch: Partial<HfEvent> = {}): HfEvent => ({
  schema_version: 1, event_id: id, agent_id: 'agent-runtime-01', deployment_id: 'decoy-medium-01', instance_id: 'cowrie-0003', session_id: 's-1',
  source_seq: 1, occurred_at: new Date().toISOString(), received_at: new Date().toISOString(), type: 'session.command', source: { ip: '203.0.113.9', port: 4000 },
  service: { protocol: 'ssh', port: 2222 }, data: { input: `cmd-${id}` }, artifact_ids: [], profile_revision: 1, ...patch,
});

function open(path: string, create: () => StreamClient<HfEvent>) {
  window.history.pushState({}, '', path);
  render(<App client={makeQueryClient()} createStream={create} />);
}

const rows = () => screen.getAllByTestId('event-row');

describe('живая лента (F10–F12)', () => {
  beforeEach(() => resetMockSession(true));

  it('живое событие появляется сверху; индикатор — «онлайн»', async () => {
    const s = controlledStream();
    open('/events', s.create);
    await screen.findAllByTestId('event-row');
    expect(await screen.findByText('онлайн')).toBeInTheDocument();
    s.send(ev('live-1'));
    expect(within(rows()[0]!).getByText('cmd-live-1')).toBeInTheDocument();
    expect(rows()).toHaveLength(4);
  });

  it('повтор события из потока не дублирует строку', async () => {
    const s = controlledStream();
    open('/events', s.create);
    await screen.findAllByTestId('event-row');   // 3 события из фикстуры
    const dup = ev('dup-1');
    s.send(dup);
    expect(rows()).toHaveLength(4);
    // Та же пара (agent_id, event_id) из потока — повтор, строка не дублируется.
    s.send(dup);
    expect(rows()).toHaveLength(4);
  });

  it('живое событие не под фильтр — не показывается', async () => {
    const s = controlledStream();
    open('/events?type=auth.attempt', s.create);
    await screen.findAllByTestId('event-row');
    expect(rows()).toHaveLength(1);
    s.send(ev('live-cmd'));
    s.send(ev('live-auth', { type: 'auth.attempt', data: { username: 'u', password: 'p' } }));
    expect(rows()).toHaveLength(2);
    expect(screen.queryByText('cmd-live-cmd')).toBeNull();
  });

  it('пауза копит новые, «показать» выводит их', async () => {
    const user = userEvent.setup();
    const s = controlledStream();
    open('/events', s.create);
    await screen.findAllByTestId('event-row');
    await user.click(screen.getByRole('button', { name: '⏸ Пауза' }));
    s.send(ev('p1'));
    s.send(ev('p2'));
    expect(rows()).toHaveLength(3);
    await user.click(screen.getByRole('button', { name: '2 новых — показать' }));
    expect(rows()).toHaveLength(5);
    expect(within(rows()[0]!).getByText('cmd-p2')).toBeInTheDocument();
  });

  it('фильтры: неверный IP подсвечен и не применяется; верный — попадает в адрес', async () => {
    const user = userEvent.setup();
    const s = controlledStream();
    open('/events', s.create);
    await screen.findAllByTestId('event-row');
    const ip = screen.getByLabelText('IP источника');
    await user.type(ip, '999.1.1.1');
    await user.click(screen.getByRole('button', { name: 'Применить' }));
    expect(screen.getByText('Нужен IPv4 или IPv6')).toBeInTheDocument();
    expect(window.location.search).toBe('');

    await user.clear(ip);
    await user.type(ip, '198.51.100.7');
    await user.click(screen.getByRole('button', { name: 'Применить' }));
    expect(window.location.search).toBe('?source_ip=198.51.100.7');
    await screen.findByText('GET /<img src=x onerror=alert(1)>');
    expect(rows()).toHaveLength(1);

    await user.click(screen.getByRole('button', { name: 'Сбросить' }));
    expect(await screen.findAllByTestId('event-row')).toHaveLength(3);
  });

  it('«Показать ещё» догружает следующую страницу', async () => {
    const user = userEvent.setup();
    for (let i = 0; i < 55; i++) publish(ev(`bulk-${i}`));
    const s = controlledStream();
    open('/events', s.create);
    await screen.findAllByTestId('event-row');
    expect(rows()).toHaveLength(50);
    await user.click(screen.getByRole('button', { name: 'Показать ещё' }));
    expect(await screen.findByText('cmd-bulk-0')).toBeInTheDocument();
    expect(rows()).toHaveLength(58);
    expect(screen.queryByRole('button', { name: 'Показать ещё' })).toBeNull();
  });
});
