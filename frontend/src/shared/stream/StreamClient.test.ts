// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest';
import { CLOSE_FORBIDDEN, CLOSE_UNAUTHORIZED, parseMessage, StreamClient, type GapPage, type StreamRecord, type StreamSocket } from './StreamClient';

type Ev = { event_id: string };

/** Сокет-подделка: тест решает, что он прислал и когда закрылся. */
class FakeSocket implements StreamSocket {
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: ((ev: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  hello(watermark: string) { this.onmessage?.({ data: JSON.stringify({ type: 'hello', watermark }) }); }
  push(seq: string, id = `e${seq}`) { this.onmessage?.({ data: JSON.stringify({ type: 'event', stream_seq: seq, event: { event_id: id } }) }); }
  drop(code = 1006) { this.onclose?.({ code }); }
  close() { this.closed = true; }
}

const rec = (seq: string, id = `e${seq}`): StreamRecord<Ev> => ({ stream_seq: seq, event: { event_id: id } });
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

/** Признак устаревшего курсора (аналог 410) для теста пересинхронизации. */
class StreamGoneError extends Error {}

function setup(opts: {
  cursor?: string | null;
  gap?: (after: string, until: string, page: string | null) => GapPage<Ev> | Promise<GapPage<Ev>>;
} = {}) {
  const sockets: FakeSocket[] = [];
  const timers: Array<{ fn: () => void; ms: number }> = [];
  const gapCalls: string[] = [];
  const calls = { unauthorized: 0, abnormal: 0 };
  const client = new StreamClient<Ev>({
    cursor: opts.cursor,
    isGone: (e) => e instanceof StreamGoneError,
    connect: () => { const s = new FakeSocket(); sockets.push(s); return s; },
    fetchGap: async (after, until, page) => {
      gapCalls.push(`${after}..${until}${page ? `#${page}` : ''}`);
      return opts.gap ? opts.gap(after, until, page) : { items: [], watermark: until, next_cursor: null };
    },
    onUnauthorized: () => { calls.unauthorized += 1; },
    onAbnormalClose: () => { calls.abnormal += 1; },
    setTimer: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimer: () => {},
  });
  const got: string[] = [];
  client.subscribe((rs) => got.push(...rs.map((r) => r.stream_seq)));
  const statuses: string[] = [];
  client.onStatus((s) => statuses.push(s));
  return { client, sockets, timers, gapCalls, got, statuses, calls };
}

describe('StreamClient: протокол hello/event', () => {
  it('первое подключение: история не догружается, курсор = watermark, дальше живые события', async () => {
    const { client, sockets, got, gapCalls, statuses } = setup();
    client.start();
    sockets[0]!.push('119');            // пришло до hello — старше watermark, отбросится
    sockets[0]!.hello('120');
    await flush();
    sockets[0]!.push('121');
    expect(gapCalls).toEqual([]);
    expect(got).toEqual(['121']);
    expect(client.currentCursor).toBe('121');
    expect(statuses).toEqual(['connecting', 'live']);
  });

  it('события сокета до конца догрузки буферизуются и сливаются по порядку без дублей', async () => {
    let release!: (p: GapPage<Ev>) => void;
    const { client, sockets, got } = setup({
      cursor: '100',
      gap: () => new Promise((resolve) => { release = resolve; }),
    });
    client.start();
    sockets[0]!.hello('102');
    sockets[0]!.push('103');
    sockets[0]!.push('102');           // повтор того, что вернёт REST
    release({ items: [rec('101'), rec('102')], watermark: '102', next_cursor: null });
    await flush();
    expect(got).toEqual(['101', '102', '103']);
  });

  it('догрузка постранично с тем же until', async () => {
    const { client, sockets, got, gapCalls } = setup({
      cursor: '0',
      gap: (_a, _u, page) => (page === null
        ? { items: [rec('1'), rec('2')], watermark: '4', next_cursor: 'p2' }
        : { items: [rec('3'), rec('4')], watermark: '4', next_cursor: null }),
    });
    client.start();
    sockets[0]!.hello('4');
    await flush();
    expect(gapCalls).toEqual(['0..4', '0..4#p2']);
    expect(got).toEqual(['1', '2', '3', '4']);
  });

  it('номера больше 2^53 сравниваются точно', async () => {
    const big = '9007199254740993'; // 2^53 + 1 — как number совпал бы с 2^53
    const { client, sockets, got } = setup({ cursor: '9007199254740992' });
    client.start();
    sockets[0]!.hello('9007199254740992');
    await flush();
    sockets[0]!.push(big);
    sockets[0]!.push('9007199254740992'); // не новее курсора
    expect(got).toEqual([big]);
    expect(client.currentCursor).toBe(big);
  });

  it('обрыв: переподключение с курсора без потерь и дублей', async () => {
    const journal = [rec('1'), rec('2'), rec('3'), rec('4'), rec('5')];
    const { client, sockets, timers, got, calls } = setup({
      gap: (after, until) => ({
        items: journal.filter((r) => BigInt(r.stream_seq) > BigInt(after) && BigInt(r.stream_seq) <= BigInt(until)),
        watermark: until, next_cursor: null,
      }),
    });
    client.start();
    sockets[0]!.hello('2');
    await flush();
    sockets[0]!.push('3');
    sockets[0]!.drop(1006);
    expect(calls.abnormal).toBe(1);
    expect(timers[0]!.ms).toBe(1000);
    timers[0]!.fn();
    sockets[1]!.push('5');               // живое до hello — в буфер
    sockets[1]!.hello('5');
    await flush();
    expect(got).toEqual(['3', '4', '5']);
  });

  it('4401 — сессия истекла: поток остановлен, приложение уводит на вход', () => {
    const { client, sockets, timers, calls, statuses } = setup();
    client.start();
    sockets[0]!.drop(CLOSE_UNAUTHORIZED);
    expect(calls.unauthorized).toBe(1);
    expect(timers).toHaveLength(0);
    expect(statuses.at(-1)).toBe('stopped');
  });

  it('4403 — нет доступа: без переподключения', () => {
    const { client, sockets, timers, statuses } = setup();
    client.start();
    sockets[0]!.drop(CLOSE_FORBIDDEN);
    expect(timers).toHaveLength(0);
    expect(statuses.at(-1)).toBe('forbidden');
  });

  it('штатное закрытие 1000 не считается сбоем сессии', () => {
    const { client, sockets, calls } = setup();
    client.start();
    sockets[0]!.drop(1000);
    expect(calls.abnormal).toBe(0);
  });

  it('задержка переподключения растёт и ограничена 30 с', () => {
    const { client, sockets, timers } = setup();
    client.start();
    for (let i = 0; i < 7; i++) { sockets.at(-1)!.drop(); timers.at(-1)!.fn(); }
    expect(timers.map((t) => t.ms)).toEqual([1000, 2000, 4000, 8000, 16000, 30000, 30000]);
  });

  it('ошибка догрузки не сдвигает курсор и ведёт к переподключению', async () => {
    let fail = true;
    const { client, sockets, timers, got } = setup({
      cursor: '0',
      gap: () => { if (fail) throw new Error('503'); return { items: [rec('1')], watermark: '1', next_cursor: null }; },
    });
    client.start();
    sockets[0]!.hello('1');
    await flush();
    expect(sockets[0]!.closed).toBe(true);
    expect(client.currentCursor).toBe('0');
    sockets[0]!.drop();
    fail = false;
    timers[0]!.fn();
    sockets[1]!.hello('1');
    await flush();
    expect(got).toEqual(['1']);
  });

  it('упавший подписчик на живом событии: курсор не сдвигается, догрузка доставляет его снова (P2-11)', async () => {
    let broken = true;
    const { client, sockets, timers } = setup({ cursor: '0', gap: () => ({ items: [rec('1')], watermark: '1', next_cursor: null }) });
    client.subscribe(() => { if (broken) throw new Error('render'); });
    client.start();
    sockets[0]!.hello('0');
    await flush();
    // Живое событие роняет подписчика — клиент не пробрасывает ошибку, а
    // закрывает сокет; курсор остаётся на прежнем значении.
    sockets[0]!.push('1');
    expect(sockets[0]!.closed).toBe(true);
    expect(client.currentCursor).toBe('0');
    // Переподключение: догрузка от прежнего курсора доставляет пропущенное.
    sockets[0]!.drop();
    broken = false;
    timers.at(-1)!.fn();
    sockets[1]!.hello('1');
    await flush();
    expect(client.currentCursor).toBe('1');
  });

  it('при падении одного подписчика остальные не получают событие повторно (P2-11)', async () => {
    let broken = true;
    const retried: string[] = [];
    const { client, sockets, got, timers } = setup({ cursor: '0', gap: () => ({ items: [rec('1')], watermark: '1', next_cursor: null }) });
    client.subscribe((rs) => { if (broken) throw new Error('render'); retried.push(...rs.map((r) => r.stream_seq)); });
    client.start();
    sockets[0]!.hello('0');
    await flush();
    sockets[0]!.push('1');          // здоровый got уже получил '1', второй упал
    sockets[0]!.drop();
    broken = false;
    timers.at(-1)!.fn();
    sockets[1]!.hello('1');
    await flush();
    // Здоровый подписчик получил '1' один раз; восстановившийся — только теперь.
    expect(got).toEqual(['1']);
    expect(retried).toEqual(['1']);
  });

  it('устаревший курсор (410): сброс курсора и сигнал пересинхронизации, без зацикливания', async () => {
    let gone = true;
    const resyncs: number[] = [];
    const { client, sockets, timers, gapCalls } = setup({
      cursor: '5',
      gap: () => { if (gone) throw new StreamGoneError(); return { items: [], watermark: '9', next_cursor: null }; },
    });
    client.onResync(() => resyncs.push(1));
    client.start();
    sockets[0]!.hello('9');
    await flush();
    // 410 сбрасывает курсор; REST ждёт hello следующего сокета.
    expect(resyncs).toEqual([]);
    expect(client.currentCursor).toBeNull();
    // Переподключение: курсор = watermark без повторной догрузки мёртвого '5'.
    gone = false;
    sockets[0]!.drop();
    timers.at(-1)!.fn();
    sockets[1]!.hello('9');
    await flush();
    expect(resyncs).toEqual([1]);
    expect(client.currentCursor).toBe('9');
    expect(gapCalls).toEqual(['5..9']);   // догрузка по мёртвому курсору была только один раз
  });

  it('новый hello запускает снимок, live во время снимка ждёт его завершения', async () => {
    let finish!: () => void;
    const { client, sockets, got } = setup();
    client.onResync(() => new Promise<void>((resolve) => { finish = resolve; }));
    client.start();
    sockets[0]!.hello('10');
    sockets[0]!.push('11');
    expect(client.currentCursor).toBeNull();
    expect(got).toEqual([]);
    finish();
    await flush();
    expect(got).toEqual(['11']);
    expect(client.currentCursor).toBe('11');
  });

  it('не применяет снимок завершившегося старого соединения', async () => {
    let finish!: () => void;
    const { client, sockets, got } = setup();
    client.onResync(() => new Promise<void>((resolve) => { finish = resolve; }));
    client.start();
    sockets[0]!.hello('10');
    sockets[0]!.push('11');
    client.stop();
    finish();
    await flush();
    expect(client.currentCursor).toBeNull();
    expect(got).toEqual([]);
    expect(client.currentStatus).toBe('stopped');
  });

  it('stop закрывает сокет', () => {
    const { client, sockets, statuses } = setup();
    client.start();
    client.stop();
    expect(sockets[0]!.closed).toBe(true);
    expect(statuses.at(-1)).toBe('stopped');
  });
});

describe('parseMessage', () => {
  it('hello и event разбираются, мусор отбрасывается', () => {
    expect(parseMessage('{"type":"hello","watermark":"7"}')).toEqual({ type: 'hello', watermark: '7' });
    expect(parseMessage('{"type":"event","stream_seq":"8","event":{"event_id":"a"}}'))
      .toEqual({ type: 'event', record: { stream_seq: '8', event: { event_id: 'a' } } });
    for (const bad of ['x', '{"type":"hello","watermark":7}', '{"type":"event","stream_seq":"-1","event":{"event_id":"a"}}',
      '{"type":"event","stream_seq":"8","event":{}}', '{"type":"ping"}', 'null']) {
      expect(parseMessage(bad)).toBeNull();
    }
  });
});
