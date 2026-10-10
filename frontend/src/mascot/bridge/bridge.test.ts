// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MascotBridge } from './MascotBridge';
import { DEFAULT_RULES } from './rules';
import { normalize, parseEvent } from './normalize';
import type { MascotCore } from '../core/MascotCore';
import type { BridgeOptions, BridgeRules } from './types';
import { ORIGINAL_FETCH } from '../assets';

/** Ядро-заглушка: записывает, что с ним сделали. */
function fakeCore() {
  const log = { states: [] as string[], said: [] as string[], clips: [] as string[] };
  const core = {
    setState: (s: string) => log.states.push(s),
    say: (t: string) => log.said.push(t),
    playGesture: (c: string) => log.clips.push(c),
    getRenderer: () => null,
  } as unknown as MascotCore;
  return { core, log };
}

function setup(rules: BridgeRules = DEFAULT_RULES, opts?: BridgeOptions) {
  const { core, log } = fakeCore();
  return { bridge: new MascotBridge(core, rules, opts), log };
}

const respond = (ms: number, status = 200) => () =>
  new Promise<Response>((r) => setTimeout(() => r(new Response(null, { status })), ms));

describe('normalize', () => {
  const p = normalize({ result: { score: '0.4', ok: 'true', name: 7 }, list: null });

  it('достаёт значения по пути и приводит типы', () => {
    expect(p.num('result.score')).toBe(0.4);
    expect(p.bool('result.ok')).toBe(true);
    expect(p.str('result.name')).toBe('7');
  });

  it('на мусоре отдаёт запасное значение и не бросает', () => {
    expect(p.num('list.deep.path', 5)).toBe(5);
    expect(normalize(null).str('a', 'x')).toBe('x');
    expect(normalize('строка').num('length', 1)).toBe(1);
    expect(p.has('nope')).toBe(false);
  });
});

describe('parseEvent', () => {
  it('понимает разные имена поля с событием', () => {
    for (const key of ['type', 'event', 'name', 'action', 'status', 'state']) {
      expect(parseEvent({ [key]: 'done' })?.name).toBe('done');
    }
  });

  it('берёт payload, затем data, иначе весь объект', () => {
    expect(parseEvent({ type: 'a', payload: 1, data: 2 })?.payload).toBe(1);
    expect(parseEvent({ type: 'a', data: 2 })?.payload).toBe(2);
  });

  it('разбирает JSON в строке и голую строку', () => {
    expect(parseEvent('{"event":"done"}')?.name).toBe('done');
    expect(parseEvent('  ready ')?.name).toBe('ready');
    expect(parseEvent('{битый')?.name).toBe('{битый');
  });

  it('на пустом и непонятном возвращает null', () => {
    for (const v of ['', null, 42, {}, { type: '' }]) expect(parseEvent(v)).toBeNull();
  });
});

describe('MascotBridge: правила', () => {
  it('строка, объект и функция', () => {
    const { bridge, log } = setup({
      a: 'thinking',
      b: { state: 'success', say: 'Готово', clip: 'nod' },
      c: (p) => (p.num('confidence') < 0.5 ? 'curious' : 'success'),
    });
    bridge.handle('a');
    bridge.handle('b');
    bridge.handle('c', { confidence: 0.2 });
    expect(log.states).toEqual(['thinking', 'success', 'curious']);
    expect(log.said).toEqual(['Готово']);
    expect(log.clips).toEqual(['nod']);
  });

  it('неизвестное событие и неизвестное состояние игнорируются', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const { bridge, log } = setup({ x: 'dancing' as never });
    bridge.handle('нет такого');
    bridge.handle('x');
    expect(log.states).toEqual([]);
    warn.mockRestore();
  });

  it('исключение в правиле не выходит наружу', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const { bridge } = setup({ boom: () => { throw new Error('x'); } });
    expect(() => bridge.handle('boom')).not.toThrow();
    warn.mockRestore();
  });

  it('уверенность модели меняет реакцию на request:done', () => {
    const { bridge, log } = setup();
    bridge.handle('request:done', { confidence: 0.2 });
    bridge.handle('request:done', { confidence: 0.6 });
    bridge.handle('request:done', { confidence: 0.9 });
    bridge.handle('request:done');
    expect(log.states).toEqual(['curious', 'gentle', 'success', 'success']);
  });
});

describe('MascotBridge: throttle', () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  it('схлопывает шторм, но последнее событие доходит', () => {
    const { bridge, log } = setup(DEFAULT_RULES, { throttleMs: 200 });
    bridge.handle('request:start');
    bridge.handle('search:start');
    bridge.handle('analyze:start');
    bridge.handle('done');
    expect(log.states).toEqual(['thinking']);
    vi.advanceTimersByTime(250);
    expect(log.states).toEqual(['thinking', 'success']);
  });

  it('dispose снимает отложенное событие', () => {
    const { bridge, log } = setup(DEFAULT_RULES, { throttleMs: 200 });
    bridge.handle('request:start');
    bridge.handle('done');
    bridge.dispose();
    vi.advanceTimersByTime(250);
    expect(log.states).toEqual(['thinking']);
  });
});

describe('MascotBridge: источники', () => {
  it('отписка снимает только свой источник', () => {
    const { bridge, log } = setup();
    const a = new EventTarget();
    const b = new EventTarget();
    const offA = bridge.listen(a, ['search:start']);
    bridge.listen(b, ['analyze:start']);
    offA();
    a.dispatchEvent(new CustomEvent('search:start'));
    b.dispatchEvent(new CustomEvent('analyze:start'));
    expect(log.states).toEqual(['analyzing']);
  });

  it('WebSocket: ошибка не дублирует реакцию на закрытие', () => {
    const { bridge, log } = setup();
    const ws = new EventTarget() as WebSocket;
    bridge.listenWebSocket(ws);
    ws.dispatchEvent(new Event('error'));
    ws.dispatchEvent(new Event('close'));
    ws.dispatchEvent(new MessageEvent('message', { data: '{"type":"search:start"}' }));
    expect(log.states).toEqual(['error', 'searching']);
  });
});

describe('MascotBridge: wrapFetch', () => {
  it('параллельные запросы — одна волна', async () => {
    const { bridge, log } = setup();
    const f = bridge.wrapFetch(((u: string) => respond(u === 'a' ? 5 : 20)()) as typeof fetch);
    await Promise.all([f('a'), f('b')]);
    expect(log.states).toEqual(['thinking', 'success']);
  });

  it('ошибка одного — error сразу и без success в конце', async () => {
    const { bridge, log } = setup();
    const f = bridge.wrapFetch(((u: string) => respond(u === 'a' ? 5 : 20, u === 'a' ? 500 : 200)()) as typeof fetch);
    await Promise.all([f('a'), f('b')]);
    expect(log.states).toEqual(['thinking', 'error']);
  });

  it('отмена запроса — не ошибка и не успех', async () => {
    const { bridge, log } = setup();
    const f = bridge.wrapFetch((() => Promise.reject(new DOMException('', 'AbortError'))) as typeof fetch);
    await expect(f('x')).rejects.toThrow();
    expect(log.states).toEqual(['thinking', 'idle']);
  });

  it('отмена одного при успехе другого — success', async () => {
    const { bridge, log } = setup();
    const f = bridge.wrapFetch(((u: string) => (u === 'a'
      ? Promise.reject(new DOMException('', 'AbortError'))
      : respond(5)())) as typeof fetch);
    await Promise.allSettled([f('a'), f('b')]);
    expect(log.states).toEqual(['thinking', 'success']);
  });

  it('сетевая ошибка пробрасывается дальше', async () => {
    const { bridge, log } = setup();
    const f = bridge.wrapFetch((() => Promise.reject(new TypeError('offline'))) as typeof fetch);
    await expect(f('x')).rejects.toThrow('offline');
    expect(log.states).toEqual(['thinking', 'error']);
  });

  it('match пропускает чужие запросы мимо', async () => {
    const { bridge, log } = setup();
    const f = bridge.wrapFetch(respond(1) as typeof fetch, { match: (u) => u.includes('/api/') });
    await f('/static/a.png');
    expect(log.states).toEqual([]);
    await f('/api/predict');
    expect(log.states).toEqual(['thinking', 'success']);
  });

  it('обёртка помечена исходным fetch — по метке её обходят ассеты', () => {
    const { bridge } = setup();
    const base = respond(1) as typeof fetch;
    const f = bridge.wrapFetch(base) as typeof fetch & { [ORIGINAL_FETCH]?: unknown };
    expect(f[ORIGINAL_FETCH]).toBe(base);
  });
});
