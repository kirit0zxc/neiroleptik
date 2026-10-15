// @vitest-environment happy-dom
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { setupServer } from 'msw/node';
import { http, HttpResponse } from 'msw';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { App, makeQueryClient } from '../../app/App';
import { handlers, resetMockSession } from '../../mocks/handlers';
import { publish } from '../../mocks/stream';
import { DEMO_SESSION } from '../../mocks/replay';
import type { HfEvent } from '../../shared/api/types';
import { ApiError } from '../../shared/api/client';
import { StreamClient, type StreamSocket } from '../../shared/stream/StreamClient';

const server = setupServer(...handlers);
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => { server.resetHandlers(); vi.unstubAllEnvs(); });
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
  beforeEach(() => { vi.stubEnv('VITE_API_MOCK', '1'); resetMockSession(true); });

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
    await screen.findByText('GET [скрыто] — полнота захвата неизвестна; ответ не записан');
    expect(screen.queryByText(/GET \/<img/)).not.toBeInTheDocument();
    expect(rows()).toHaveLength(1);

    await user.click(screen.getByRole('button', { name: 'Сбросить' }));
    expect(await screen.findAllByTestId('event-row')).toHaveLength(3);
  });

  it('событие с сессией ведёт на её запись, без сессии — ссылки нет', async () => {
    publish(ev('with-session', { session_id: DEMO_SESSION }));
    publish(ev('no-session', { session_id: null }));
    open('/events', controlledStream().create);
    const withRow = (await screen.findByText('cmd-with-session')).closest('tr')!;
    expect(within(withRow).getByRole('link', { name: `Запись сессии …${DEMO_SESSION.slice(-8)}` }))
      .toHaveAttribute('href', `/sessions/${DEMO_SESSION}`);
    const noRow = screen.getByText('cmd-no-session').closest('tr')!;
    expect(within(noRow).queryByRole('link')).toBeNull();
  });

  it('без включённой записи сессии ссылки «Запись» нет — не ведём на заглушку', async () => {
    vi.stubEnv('VITE_API_MOCK', '0');
    vi.stubEnv('VITE_EVENTS_ENABLED', '1');
    vi.stubEnv('VITE_REPLAY_ENABLED', '0');
    vi.stubEnv('VITE_TELEMETRY_ENABLED', '0');
    publish(ev('no-replay', { session_id: DEMO_SESSION }));
    open('/events', controlledStream().create);
    const row = (await screen.findByText('cmd-no-replay')).closest('tr')!;
    expect(within(row).queryByRole('link')).toBeNull();
  });

  it('сессии с общим началом UUID (одна минута, UUIDv7) различаются по имени ссылки', async () => {
    publish(ev('v7-a', { session_id: '0192ab3c-0000-7000-8000-00000000aaaa' }));
    publish(ev('v7-b', { session_id: '0192ab3c-0000-7000-8000-00000000bbbb' }));
    open('/events', controlledStream().create);
    const a = (await screen.findByText('cmd-v7-a')).closest('tr')!;
    const b = screen.getByText('cmd-v7-b').closest('tr')!;
    expect(within(a).getByRole('link')).toHaveAccessibleName('Запись сессии …0000aaaa');
    expect(within(b).getByRole('link')).toHaveAccessibleName('Запись сессии …0000bbbb');
  });

  it('событие без источника не роняет ленту: «источник неизвестен»', async () => {
    // Защита от неполных данных: по схеме source обязателен, но приходит от ловушек.
    publish({ ...ev('no-source'), source: null } as unknown as HfEvent);
    open('/events', controlledStream().create);
    const row = (await screen.findByText('cmd-no-source')).closest('tr')!;
    expect(within(row).getByText('источник неизвестен')).toBeInTheDocument();
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

  it('422 на «Показать ещё»: история перечитывается с первой страницы, с пометкой', async () => {
    const user = userEvent.setup();
    for (let i = 0; i < 55; i++) publish(ev(`stale-${i}`));
    open('/events', controlledStream().create);
    await screen.findAllByTestId('event-row');
    let firstPages = 0;
    server.use(http.get('/api/v1/events', ({ request }) => {
      if (new URL(request.url).searchParams.get('cursor')) return HttpResponse.json({ detail: 'Курсор не принимается' }, { status: 422 });
      firstPages++;
      return undefined;
    }));
    await user.click(screen.getByRole('button', { name: 'Показать ещё' }));
    expect(await screen.findByText('Список событий обновлён — загружен заново.')).toBeInTheDocument();
    await waitFor(() => expect(rows()).toHaveLength(50));
    expect(firstPages).toBe(1);
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('422 на «Показать ещё» на паузе: показанный live остаётся, очередь паузы не появляется сама', async () => {
    const user = userEvent.setup();
    for (let i = 0; i < 55; i++) publish(ev(`keep-${i}`));
    const s = controlledStream();
    open('/events', s.create);
    await screen.findByText('онлайн');
    await screen.findAllByTestId('event-row');
    publish(ev('shown')); s.send(ev('shown'));
    await screen.findByText('cmd-shown');
    await user.click(screen.getByRole('button', { name: '⏸ Пауза' }));
    publish(ev('waiting')); s.send(ev('waiting'));
    server.use(http.get('/api/v1/events', ({ request }) => (new URL(request.url).searchParams.get('cursor')
      ? HttpResponse.json({ detail: 'Курсор не принимается' }, { status: 422 }) : undefined)));
    await user.click(screen.getByRole('button', { name: 'Показать ещё' }));
    await screen.findByText('Список событий обновлён — загружен заново.');
    await waitFor(() => expect(screen.getByText('cmd-shown')).toBeInTheDocument());
    expect(screen.queryByText('cmd-waiting')).toBeNull();
    expect(screen.getByRole('button', { name: '1 новых — показать' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '▶ Продолжить' })).toHaveAttribute('aria-pressed', 'true');
  });

  it('повторный 422 после автоматического перечитывания — ошибка, а не новый сброс', async () => {
    const user = userEvent.setup();
    for (let i = 0; i < 55; i++) publish(ev(`again-${i}`));
    open('/events', controlledStream().create);
    await screen.findAllByTestId('event-row');
    let firstPages = 0;
    server.use(http.get('/api/v1/events', ({ request }) => {
      if (new URL(request.url).searchParams.get('cursor')) return HttpResponse.json({ detail: 'Курсор не принимается' }, { status: 422 });
      firstPages++;
      return undefined;
    }));
    await user.click(screen.getByRole('button', { name: 'Показать ещё' }));
    await screen.findByText('Список событий обновлён — загружен заново.');
    await waitFor(() => expect(rows()).toHaveLength(50));
    await user.click(screen.getByRole('button', { name: 'Показать ещё' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Курсор не принимается');
    expect(firstPages).toBe(1);
  });

  it('история в порядке центра: received_at, затем event_id по убыванию', async () => {
    const t = '2026-10-10T09:00:00.000Z';
    publish(ev('ord-a', { received_at: t }));
    publish(ev('ord-c', { received_at: t }));
    publish(ev('ord-b', { received_at: '2026-10-10T08:00:00.000Z' }));
    open('/events?type=session.command', controlledStream().create);
    await screen.findByText('cmd-ord-a');
    const order = rows().map((r) => r.textContent ?? '').filter((t) => t.includes('cmd-ord-')).map((t) => /cmd-(ord-\w)/.exec(t)![1]);
    expect(order.indexOf('ord-c')).toBeLessThan(order.indexOf('ord-a'));
    expect(order.indexOf('ord-a')).toBeLessThan(order.indexOf('ord-b'));
  });

  it('догрузка стабильного снимка на паузе сохраняет ранее показанный live', async () => {
    const user = userEvent.setup();
    for (let i = 0; i < 55; i++) publish(ev(`history-${i}`));
    const s = controlledStream();
    open('/events', s.create);
    await screen.findByText('онлайн');
    await screen.findAllByTestId('event-row');
    publish(ev('shown'));
    s.send(ev('shown'));
    await screen.findByText('cmd-shown');
    await user.click(screen.getByRole('button', { name: '⏸ Пауза' }));
    publish(ev('pending'));
    s.send(ev('pending'));
    await user.click(screen.getByRole('button', { name: 'Показать ещё' }));
    await screen.findByText('cmd-history-0');
    expect(screen.getByText('cmd-shown')).toBeInTheDocument();
    expect(screen.queryByText('cmd-pending')).toBeNull();
    expect(screen.getByRole('button', { name: '1 новых — показать' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '▶ Продолжить' })).toHaveAttribute('aria-pressed', 'true');
  });

  it('переполнение не скрывается: продолжение обновляет снимок и позволяет найти вытесненное событие', async () => {
    const user = userEvent.setup();
    const s = controlledStream();
    open('/events', s.create);
    await screen.findByText('онлайн');
    await screen.findAllByTestId('event-row');
    await user.click(screen.getByRole('button', { name: '⏸ Пауза' }));
    act(() => {
      for (let i = 0; i < 501; i++) { const event = ev(`overflow-${i}`); publish(event); s.send(event); }
    });
    expect(screen.getByRole('alert')).toHaveTextContent('1 событий требуют повторной загрузки');
    await user.click(screen.getByRole('button', { name: '▶ Продолжить' }));
    await screen.findByText('cmd-overflow-500');
    expect(screen.queryByRole('alert')).toBeNull();
    // Новая выборка содержит и запись, не поместившуюся в pending.
    for (let page = 1; page <= 10; page++) {
      await user.click(screen.getByRole('button', { name: 'Показать ещё' }));
      await waitFor(() => expect(rows()).toHaveLength(Math.min((page + 1) * 50, 504)));
    }
    expect(screen.getByText('cmd-overflow-0')).toBeInTheDocument();
  }, 15000);

  it('после 410 получает новый hello до REST, события во время снимка не теряются', async () => {
    let reads = 0;
    let release!: () => void;
    const waiting = new Promise<void>((resolve) => { release = resolve; });
    server.use(http.get('/api/v1/events', async () => {
      reads += 1;
      if (reads > 1) await waiting;
      return HttpResponse.json({ items: reads === 1 ? [ev('9')] : [ev('10'), ev('9')], next_cursor: null });
    }));
    const sockets: StreamSocket[] = [];
    const timers: Array<() => void> = [];
    let stream!: StreamClient<HfEvent>;
    const create = () => (stream = new StreamClient<HfEvent>({
      cursor: '5',
      connect: () => {
        const socket: StreamSocket = { onopen: null, onmessage: null, onclose: null, onerror: null, close: () => socket.onclose?.({ code: 1000 }) };
        sockets.push(socket);
        return socket;
      },
      fetchGap: async () => { throw new ApiError(410, 'История утрачена'); },
      isGone: (error) => error instanceof ApiError && error.status === 410,
      setTimer: (fn) => { timers.push(fn); return timers.length; }, clearTimer: () => {},
    }));
    try {
      open('/events', create);
      await screen.findByText('cmd-9');
      act(() => sockets[0]!.onmessage?.({ data: JSON.stringify({ type: 'hello', watermark: '9' }) }));
      await waitFor(() => expect(timers).toHaveLength(1));
      expect(reads).toBe(1); // Нельзя читать снимок до нового handshake.
      act(() => { timers[0]!(); sockets[1]!.onmessage?.({ data: JSON.stringify({ type: 'hello', watermark: '10' }) }); });
      await waitFor(() => expect(reads).toBe(2));
      act(() => sockets[1]!.onmessage?.({ data: JSON.stringify({ type: 'event', stream_seq: '11', event: ev('11') }) }));
      expect(screen.queryByText('cmd-11')).toBeNull();
      release();
      expect(await screen.findByText('cmd-10')).toBeInTheDocument();
      expect(await screen.findByText('cmd-11')).toBeInTheDocument();
      expect(stream.currentCursor).toBe('11');
    } finally { release(); }
  });
});
it('повтор после 422 и ошибки REST сохраняет очередь паузы', async () => {
  vi.stubEnv('VITE_API_MOCK','1'); resetMockSession(true);
  const pending=ev('review-retry'); let expired=false; let fail=true;
  server.use(http.get('/api/v1/events',({request})=>{
    if(new URL(request.url).searchParams.has('cursor')) {expired=true;return HttpResponse.json({detail:'expired'},{status:422});}
    if(expired && fail) return HttpResponse.json({detail:'temporary recovery error'},{status:503});
    return HttpResponse.json({items:expired?[pending,ev('old')]:[ev('old')],next_cursor:'expired'});
  }));
  const s=controlledStream(); const user=userEvent.setup();const client=makeQueryClient();client.setDefaultOptions({queries:{retry:false}});
  window.history.pushState({},'','/events');render(<App client={client} createStream={s.create}/>);
  await screen.findByText('онлайн');await screen.findByText('cmd-old');
  await user.click(screen.getByRole('button',{name:'⏸ Пауза'}));s.send(pending);
  expect(screen.getByRole('button',{name:'1 новых — показать'})).toBeInTheDocument();
  await user.click(screen.getByRole('button',{name:'Показать ещё'}));
  await waitFor(()=>expect(screen.getAllByRole('button',{name:'Повторить'})).toHaveLength(1));
  expect(screen.getByRole('button',{name:'1 новых — показать'})).toBeInTheDocument();
  fail=false;await user.click(screen.getAllByRole('button',{name:'Повторить'})[0]!);
  await waitFor(()=>expect(screen.queryAllByRole('button',{name:'Повторить'})).toHaveLength(0));
  expect(screen.queryByText('cmd-review-retry')).toBeNull();
  expect(screen.getByRole('button',{name:'1 новых — показать'})).toBeInTheDocument();
});
